from datetime import datetime, timedelta, timezone

import pytest

from loopedin import bot as botmod
from loopedin import digest, telegram
from loopedin.config import Account, Config
from loopedin.models import Email
from loopedin.sorter import SortRun
from loopedin.store import Sort, Store, key_of

ME = "me@gmail.com"
CHAT = 4242


def mail(i, minutes_ago=5, **kw) -> Email:
    return Email(account=ME, provider="gmail", id=str(i), thread_id=str(i), message_id=f"<{i}@x>",
                 subject=kw.pop("subject", f"Subject {i}"), sender_name=kw.pop("name", f"Person {i}"),
                 sender_addr=f"p{i}@x.com", to=[ME],
                 date=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), body_text="hi", **kw)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text_html, reply_markup=None):
        self.sent.append((chat_id, text_html))
        self.markup = reply_markup
        return [{"message_id": 500 + len(self.sent)}]

    def edit(self, chat_id, message_id, text_html, reply_markup=None):
        self.edited = (chat_id, message_id, text_html, reply_markup)

    def answer(self, callback_id, text=""):
        self.answered = (callback_id, text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(accounts=[Account("gmail", ME)], telegram_chat_id=CHAT, telegram_bot="my_bot")
    monkeypatch.setattr(botmod.Config, "load", classmethod(lambda cls: config))
    store = Store(tmp_path / "t.db")
    tg = FakeTelegram()
    b = botmod.Bot(store=store, tg=tg, log=lambda m: None)
    yield b, store, tg, monkeypatch
    store.close()


def fake_run(store, pairs):
    """Stand-in for sorter.run: save the emails and sorts, return them."""
    def run(config, s, since, **kw):
        s.save_emails([e for e, _ in pairs])
        for e, srt in pairs:
            s.save_sort(key_of(e), srt)
        return SortRun(items=pairs)
    return run


# -- scheduling -------------------------------------------------------------
def local(h, m=0, day=0):
    base = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
    return base + timedelta(days=day)


def test_due_slot_fires_once_per_slot():
    times = ["08:00", "13:00", "18:00"]
    now = local(13, 5)
    assert botmod.due_slot(now, times, None) == local(13)
    assert botmod.due_slot(now, times, local(13)) is None
    assert botmod.due_slot(now, times, local(8)) == local(13)


def test_due_slot_catches_up_after_the_laptop_was_off_overnight():
    assert botmod.due_slot(local(7, 30), ["08:00", "18:00"], local(18, day=-2)) == local(18, day=-1)


def test_next_slot_rolls_to_tomorrow():
    assert botmod.next_slot(local(19), ["08:00", "18:00"]) == local(8, day=1)


# -- telegram helpers -------------------------------------------------------
def test_split_message_respects_limit_on_line_boundaries():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = telegram.split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text


def test_html_digest_escapes_email_content():
    d = digest.compose([(mail(1, name="<script>", subject="a & b"), Sort("reply", True, "Pay <now> & more", "x < y"))],
                       datetime.now(timezone.utc))
    out = digest.render(d)
    assert "&lt;script&gt;" in out and "Pay &lt;now&gt; &amp; more" in out and "<i>↳ x &lt; y</i>" in out


# -- urgent pings -----------------------------------------------------------
def test_urgent_ping_once_and_only_for_new_mail(env):
    b, store, tg, mp = env
    store.set_meta("alerts_since", (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat())
    old = mail(1, minutes_ago=120)          # arrived before the bot started: digest only
    new = mail(2, minutes_ago=5, name="Sam")
    calm = mail(3, minutes_ago=5)
    pairs = [(old, Sort("reply", True, "old")), (new, Sort("reply", True, "Reference needed")),
             (calm, Sort("read", False, "fine"))]
    mp.setattr(botmod.sorter, "run", fake_run(store, pairs))

    b.check_mail()
    b.check_mail()
    assert len(tg.sent) == 1
    chat, text = tg.sent[0]
    assert chat == CHAT and "Sam" in text and "Reference needed" in text


def test_errors_are_reported_once_until_they_change(env):
    b, store, tg, mp = env
    mp.setattr(botmod.sorter, "run", lambda *a, **k: SortRun(errors=["gmail: login failed"]))
    b.check_mail()
    b.check_mail()
    assert len(tg.sent) == 1 and "login failed" in tg.sent[0][1]


# -- digests ----------------------------------------------------------------
def test_digest_includes_each_email_once_and_remembers_numbers(env):
    b, store, tg, mp = env
    pairs = [(mail(1), Sort("reply", False, "Sign contract")), (mail(2), Sort("read", False, "Photos")),
             (mail(3), Sort("fyi", False)), (mail(4), Sort("noise", False))]
    store.save_emails([e for e, _ in pairs])
    for e, s in pairs:
        store.save_sort(key_of(e), s)

    assert b.send_digest() is True
    assert len(tg.sent) == 1                                  # one message; to read + FYI behind buttons
    assert "1 needs you" in tg.sent[0][1] and "Photos" not in tg.sent[0][1]
    assert [x["text"] for x in tg.markup["inline_keyboard"][-1]] == ["📖 To read (1)", "📦 FYI (1)"]
    assert store.digest_numbers() == {1: "gmail:1", 2: "gmail:2", 3: "gmail:3"}

    # tapping "To read" expands the same message in place
    data = tg.markup["inline_keyboard"][-1][0]["callback_data"]
    b.handle({"update_id": 5, "callback_query": {"id": "cb1", "data": data,
                                                 "message": {"chat": {"id": CHAT}, "message_id": 77}}})
    chat, message_id, text, markup = tg.edited
    assert (chat, message_id) == (CHAT, 77) and "📖 To read (1)" in text and "Photos" in text
    assert markup["inline_keyboard"][-1][0]["text"] == "▾ Hide to read"
    assert tg.answered == ("cb1", "")

    assert b.send_digest() is False          # nothing new, scheduled: stay quiet
    assert len(tg.sent) == 1
    b.send_digest(on_demand=True)            # asked for it: the whole open digest again
    assert "1 needs you" in tg.sent[-1][1]


def test_scheduled_digest_skips_when_only_fyi_and_carries_counts_forward(env):
    b, store, tg, mp = env
    store.save_emails([mail(1)])
    store.save_sort("gmail:1", Sort("fyi", False))
    assert b.send_digest() is False and not tg.sent
    store.save_emails([mail(2)])
    store.save_sort("gmail:2", Sort("reply", False, "Needs you"))
    b.send_digest()
    assert tg.markup["inline_keyboard"][-1][-1]["text"] == "📦 FYI (1)"


# -- commands ---------------------------------------------------------------
def test_messages_from_other_chats_are_ignored(env):
    b, store, tg, mp = env
    b.handle({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/status"}})
    assert tg.sent == []
    b.handle({"update_id": 2, "message": {"chat": {"id": CHAT}, "text": "/status"}})
    assert "Running" in tg.sent[-1][1]


class FakeHTTP:
    def __init__(self, result):
        self.result, self.calls = result, []

    def post(self, url, json, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], json, timeout))

        class R:
            status_code = 200

            def json(_):
                return {"ok": True, "result": self.result}
        return R()


def test_long_poll_sends_telegram_timeout_and_waits_longer_over_http():
    http = FakeHTTP([])
    telegram.Telegram("1:abc", session=http).updates(offset=7, wait=25)
    method, params, http_timeout = http.calls[0]
    assert method == "getUpdates" and params["timeout"] == 25 and params["offset"] == 7
    assert http_timeout == 35


def test_pairing_accepts_only_the_code_from_a_private_chat():
    http = FakeHTTP([
        {"update_id": 1, "message": {"chat": {"id": 5, "type": "group"}, "text": "/start 123456"}},
        {"update_id": 2, "message": {"chat": {"id": 6, "type": "private"}, "text": "/start 999999"}},
        {"update_id": 3, "message": {"chat": {"id": 7, "type": "private"}, "text": "/start 123456"}},
    ])
    chat, offset = telegram.wait_for_pairing(telegram.Telegram("1:abc", session=http), "123456")
    assert (chat, offset) == (7, 4)
    tg = telegram.Telegram("1:abc", session=http)
    tg.call("getUpdates", offset=4, timeout=0)          # the acknowledge call used after pairing
    assert http.calls[-1][1] == {"offset": 4, "timeout": 0}


def test_done_on_an_urgent_ping_dismisses_and_strikes_it(env):
    b, store, tg, mp = env
    e = mail(1, name="Google")
    store.save_emails([e])
    store.save_sort("gmail:1", Sort("reply", True, "Security alert"))
    b.handle({"update_id": 9, "callback_query": {"id": "cb", "data": "dn:gmail:1",
                                                 "message": {"chat": {"id": CHAT}, "message_id": 42}}})
    assert store.dismissed(["gmail:1"]) == {"gmail:1"}
    assert tg.edited[1] == 42 and "<s>Google" in tg.edited[2]
    assert b.send_digest() is False                      # dismissed mail never reaches a digest


def test_dismissing_an_email_restrikes_the_digest_it_was_in(env):
    b, store, tg, mp = env
    pairs = [(mail(1, name="Google"), Sort("reply", True, "Security alert")), (mail(2, name="Sam"), Sort("reply", True, "Ref"))]
    store.save_emails([e for e, _ in pairs])
    for e, s in pairs:
        store.save_sort(key_of(e), s)
    b.send_digest()
    b.replies.dismiss("gmail:1")
    chat, message_id, text, markup = tg.edited
    assert message_id == 501 and "✓ <s>2. Google</s>" in text and "1 needs you" in text
    assert [x["text"] for x in markup["inline_keyboard"][0]] == ["✍️ 1"]


def test_free_text_goes_to_the_assistant_and_its_actions_run(env):
    b, store, tg, mp = env
    store.save_emails([mail(1, name="E14 Fund")])
    store.save_sort("gmail:1", Sort("fyi", False, "Hackathon"))
    tg.call = lambda *a, **k: None
    asked = []
    mp.setattr(botmod.assistant, "ask", lambda s, text, accounts: asked.append(text) or botmod.assistant.Result(
        "It's Oct 30 to Nov 1 at the Media Lab.", "dismiss", ["gmail:1"], ""))
    b.handle({"update_id": 3, "message": {"chat": {"id": CHAT}, "text": "Tell me about the E14 hack"}})
    assert asked == ["Tell me about the E14 hack"]
    assert "Media Lab" in tg.sent[0][1] and store.dismissed(["gmail:1"]) == {"gmail:1"}



def test_on_demand_digest_skips_dismissed_and_says_all_clear_when_empty(env):
    b, store, tg, mp = env
    store.save_emails([mail(1, name="Google"), mail(2, name="Sam")])
    store.save_sort("gmail:1", Sort("reply", True, "Security alert"))
    store.save_sort("gmail:2", Sort("reply", True, "Reference"))
    store.dismiss("gmail:1")
    b.send_digest(on_demand=True)
    assert "Sam" in tg.sent[-1][1] and "Google" not in tg.sent[-1][1]
    store.dismiss("gmail:2")
    b.send_digest(on_demand=True)
    assert "All clear" in tg.sent[-1][1]


def test_assistant_can_ask_for_the_real_digest(env):
    b, store, tg, mp = env
    store.save_emails([mail(1, name="Sam")])
    store.save_sort("gmail:1", Sort("reply", True, "Reference"))
    tg.call = lambda *a, **k: None
    mp.setattr(botmod.sorter, "run", lambda *a, **k: SortRun())
    mp.setattr(botmod.assistant, "ask", lambda *a: botmod.assistant.Result("Here it is.", "show_digest", [], ""))
    b.handle({"update_id": 4, "message": {"chat": {"id": CHAT}, "text": "show me my digest"}})
    assert tg.sent[0][1] == "Here it is." and "1 needs you" in tg.sent[1][1]


def test_model_html_is_limited_to_bold_and_italic():
    out = telegram.safe_html("<b>When:</b> Oct 30 <script>x</script> & <i>soon</i>")
    assert out == "<b>When:</b> Oct 30 &lt;script&gt;x&lt;/script&gt; &amp; <i>soon</i>"



def test_model_answers_get_a_blank_line_between_lines():
    assert botmod.spaced("It's on Oct 30.\n• <b>Where:</b> Media Lab\n\n\n• <b>Apply:</b> link") == \
        "It's on Oct 30.\n\n• <b>Where:</b> Media Lab\n\n• <b>Apply:</b> link"


def test_chat_transcript_records_both_sides(env, monkeypatch):
    b, store, tg, _ = env
    monkeypatch.setattr(b, "ask_assistant", lambda text: None)
    b.handle({"message": {"chat": {"id": CHAT}, "text": "what's #2 about?"}})
    b._record_sent("bot", "<b>It's</b> a hack", {"inline_keyboard": [[{"text": "✍️ Reply", "callback_data": "rk:x"}]]})
    rows = [(r["who"], r["text"]) for r in store.chat()]
    assert rows == [("you", "what's #2 about?"), ("bot", "<b>It's</b> a hack\n[buttons: ✍️ Reply]")]
