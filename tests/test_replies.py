import email
import json
from datetime import datetime, timedelta, timezone
from email import policy

import pytest

from loopedin import drafting, replies
from loopedin.config import Account, Config
from loopedin.models import Email
from loopedin.store import Sort, Store

ME = "me@gmail.com"
CHAT = 4242
NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)


def mail(i=1, **kw) -> Email:
    return Email(account=ME, provider="gmail", id=str(i), thread_id="1790", message_id=f"<m{i}@acme.ai>",
                 subject=kw.pop("subject", "Want to help shape the future of AI?"), sender_name="Sam Rivera",
                 sender_addr="sam@acme.ai", to=[ME], date=NOW, body_text=kw.pop("body", "Who's your reference?"),
                 references=["<first@acme.ai>"], **kw)


# -- the draft itself -------------------------------------------------------
def test_mime_threads_onto_the_original_and_honours_reply_to():
    raw, message_id = drafting.as_mime(mail(reply_to="talent@acme.ai"), "Hi Sam,\n\nSure.\n\nMide", "Alex Doe")
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg["To"] == "talent@acme.ai"
    assert msg["From"] == "Alex Doe <me@gmail.com>"
    assert msg["Subject"] == "Re: Want to help shape the future of AI?"
    assert msg["In-Reply-To"] == "<m1@acme.ai>"
    assert msg["References"] == "<first@acme.ai> <m1@acme.ai>"
    assert msg["Message-ID"] == message_id and message_id.endswith("@gmail.com>")
    assert msg.get_content().strip().endswith("Mide")


def test_subject_is_not_double_prefixed():
    assert drafting.reply_subject("RE: hello") == "RE: hello"


def test_prompt_has_notes_examples_rules_and_feedback_and_marks_your_messages():
    earlier = Email(account=ME, provider="gmail", id="0", thread_id="1790", message_id="", subject="s",
                    sender_name="Me", sender_addr=ME, date=NOW - timedelta(days=1), body_text="Excited to chat!")
    sent_before = Email(account=ME, provider="gmail", id="9", thread_id="x", message_id="", subject="s",
                        sender_name="Me", sender_addr=ME, date=NOW - timedelta(days=30), body_text="Hey Sam, sounds good")
    prompt = drafting.build_prompt(email=mail(), thread=[earlier], examples=[sent_before], guide="- signs off '– Mide'",
                                   rules=["never use Best,"], feedback=["shorter"], notes="ref is Jane, jane@acme.com",
                                   me={ME}, my_name="Mide")
    assert "ref is Jane, jane@acme.com" in prompt
    assert "Hey Sam, sounds good" in prompt and "- never use Best," in prompt and "- shorter" in prompt
    assert '<message from="you"' in prompt
    assert "previous draft" not in prompt.lower()


def test_gmail_link_opens_the_conversation_or_drafts():
    assert drafting.gmail_link(ME, "18f2a") ==         "https://mail.google.com/mail/mu/mp/?authuser=me%40gmail.com#cv/Drafts/18f2a"
    assert drafting.gmail_link(ME, "").endswith("#tl/Drafts")


# -- routing your messages --------------------------------------------------
class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.next_id = [], [], 100

    def send(self, chat, text, reply_markup=None):
        self.sent.append(text)
        self.next_id += 1
        return [{"message_id": self.next_id}]

    def edit(self, chat, message_id, text, reply_markup=None):
        self.edits.append((message_id, text, reply_markup))


class FakeBot:
    def __init__(self, store):
        self.store, self.tg = store, FakeTG()
        self.config = Config(accounts=[Account("gmail", ME)], telegram_chat_id=CHAT)
        self.logs = []

    def say(self, text):
        self.tg.send(CHAT, text)

    def log(self, m):
        self.logs.append(m)

    def refresh_digest_for(self, key):
        self.refreshed = key

    def refresh_digest(self, digest_id):
        self.refreshed_digests = getattr(self, "refreshed_digests", []) + [digest_id]


@pytest.fixture
def flow(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    e = mail()
    store.save_emails([e])
    store.save_sort("gmail:1", Sort("reply", True, "Reference needed"))
    store.record_digest({"gmail:1": 1}, [])
    bot = FakeBot(store)
    r = replies.Replies(bot)
    written = []
    monkeypatch.setattr(r, "_write", lambda email, notes, previous=None, change=None: written.append(
        (email.id, notes, previous, change)))
    yield r, bot, store, written
    store.close()


def test_number_prefix_drafts_a_reply_to_that_digest_item(flow):
    r, bot, store, written = flow
    assert r.on_text("1: my reference is Jane, jane@acme.com") is True
    assert written == [("1", "my reference is Jane, jane@acme.com", None, None)]


def test_unknown_number_says_so(flow):
    r, bot, store, written = flow
    assert r.on_text("7) hello") is True
    assert written == [] and "no #7" in bot.tg.sent[-1]


def test_tapping_reply_then_typing_uses_that_email(flow):
    r, bot, store, written = flow
    r.choose("gmail:1")
    assert "Sam Rivera" in bot.tg.sent[-1]
    assert r.on_text("tell him Thursday works") is True
    assert written[-1][:2] == ("1", "tell him Thursday works")
    assert r.on_text("random chatter") is False          # the pending reply was used up


def test_change_request_is_remembered_as_feedback_and_redrafts(flow, monkeypatch):
    r, bot, store, written = flow
    monkeypatch.setattr(r, "_discard_gmail_draft", lambda row: None)
    draft_id = store.add_draft("gmail:1", "ref is Jane", "Hi Sam, ...", "<d1@gmail.com>", "https://x")
    assert r.on_button(f"dr:{draft_id}:change", CHAT, 55) == ""
    assert r.on_text("shorter please") is True
    assert written[-1] == ("1", "ref is Jane", "Hi Sam, ...", "shorter please")
    assert store.notes("feedback") == ["shorter please"]
    assert store.draft(draft_id)["status"] == "replaced"


def test_skip_removes_the_gmail_draft_and_closes_it(flow, monkeypatch):
    r, bot, store, written = flow
    discarded = []
    monkeypatch.setattr(r, "_discard_gmail_draft", lambda row: discarded.append(row["id"]))
    draft_id = store.add_draft("gmail:1", "n", "b", "<d1@gmail.com>", "https://x")
    assert r.on_button(f"dr:{draft_id}:skip", CHAT, 55) == "Skipped"
    assert discarded == [draft_id] and store.draft(draft_id)["status"] == "skipped"
    assert r.on_button(f"dr:{draft_id}:skip", CHAT, 55) == "That draft is closed."


def test_style_rule_is_saved(flow):
    r, bot, store, written = flow
    assert r.on_text("style: never sign off with Best,") is True
    assert store.notes("rule") == ["never sign off with Best,"]


def test_pending_reply_expires(flow):
    r, bot, store, written = flow
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.set_meta("pending", json.dumps({"kind": "reply", "value": "gmail:1", "at": an_hour_ago}))
    assert r.on_text("hello?") is False


# -- dismissing --------------------------------------------------------------
def test_saying_dismiss_while_replying_dismisses_instead_of_drafting(flow, monkeypatch):
    r, bot, store, written = flow
    monkeypatch.setattr(r, "_discard_gmail_draft", lambda row: None)
    r.choose("gmail:1")
    assert r.on_text("Dismiss this urgent message") is True
    assert written == [] and store.dismissed(["gmail:1"]) == {"gmail:1"}
    assert bot.refreshed == "gmail:1"


def test_number_done_dismisses_and_closes_open_drafts(flow, monkeypatch):
    r, bot, store, written = flow
    discarded = []
    monkeypatch.setattr(r, "_discard_gmail_draft", lambda row: discarded.append(row["id"]))
    draft_id = store.add_draft("gmail:1", "n", "b", "<d1@gmail.com>", "https://x")
    assert r.on_text("1 done") is True
    assert written == [] and discarded == [draft_id] and store.draft(draft_id)["status"] == "skipped"
    assert store.undigested(NOW - timedelta(days=1), {ME}) == []


def test_no_reply_senders_get_done_but_no_reply_prompt(flow):
    r, bot, store, written = flow
    store.save_emails([mail(2, reply_to="no-reply@accounts.google.com")])
    r.choose("gmail:2")
    assert "doesn't read replies" in bot.tg.sent[-1]
    assert r.on_text("hello") is False          # nothing pending


def test_mit_reply_link_carries_who_it_is_from_and_the_reply_to_copy():
    link = drafting.outlook_link(mail(via="mit", reply_to="prof@mit.edu"), "Hi Prof,\n\nThursday works!\n\nMide.")
    assert link.startswith(drafting.OPEN_PAGE + "#from=Sam%20Rivera&to=prof%40mit.edu")
    assert "subject=Want%20to%20help" in link and "body=Hi%20Prof%2C%0A%0AThursday%20works%21" in link


def test_long_bodies_are_left_out_of_the_outlook_link():
    link = drafting.outlook_link(mail(via="mit"), "x" * 5000)
    assert "body=" not in link and len(link) < drafting.MAX_LINK


def test_outlook_draft_card_has_copyable_text_and_the_outlook_button():
    card = replies.render_draft(mail(via="mit"), drafting.Draft("Hi <Sam>", ""), "me@mit.edu")
    assert "From <b>me@mit.edu</b>" in card and "<pre>Hi &lt;Sam&gt;</pre>" in card
    keyboard = replies.draft_keyboard(3, "https://x/open.html#a", copy="Hi Sam")
    assert keyboard["inline_keyboard"][0] == [{"text": "📋 Copy", "copy_text": {"text": "Hi Sam"}},
                                              {"text": "↗ Outlook", "url": "https://x/open.html#a"}]


def test_long_outlook_replies_skip_the_copy_button():
    keyboard = replies.draft_keyboard(3, "https://x/open.html#a", copy="x" * 300)
    assert keyboard["inline_keyboard"][0] == [{"text": "↗ Outlook", "url": "https://x/open.html#a"}]
    card = replies.render_draft(mail(via="mit"), drafting.Draft("x" * 300, ""), "me@mit.edu")
    assert "Tap Copy on the reply above" in card



def test_done_accepts_several_numbers():
    for text, numbers in [("2 done", "2"), ("1, 3 done", "1, 3"), ("1 and 2 dismiss", "1 and 2"),
                          ("done 4", "4"), ("#1 #2 done", "#1 #2")]:
        m = replies.NUMBER_DONE.match(text)
        assert m and bool(m.group(1)) != bool(m.group(3)) and m.group(2) == numbers, text
    m = replies.NUMBER_DONE.match("2")
    assert not (m and bool(m.group(1)) != bool(m.group(3)))


def test_lgtm_keeps_the_draft_instead_of_rewriting():
    for text in ["Lgtm", "looks good!", "ok", "Perfect", "no changes"]:
        assert replies.KEEP.match(text), text
    assert not replies.KEEP.match("make it shorter")
