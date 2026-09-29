from datetime import datetime, timedelta, timezone

import pytest

from loopedin import classify, digest, rules, sorter
from loopedin.claude import LimitReached
from loopedin.config import Account, Config
from loopedin.inbox import AccountResult
from loopedin.models import Email
from loopedin.store import Sort, Store, key_of
from loopedin.text import strip_quoted

ME = "me@gmail.com"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def mail(i, sender="sam@studio.co", subject="Contract", body="Can you sign by Friday?", **kw) -> Email:
    return Email(account=ME, provider="gmail", id=str(i), thread_id=f"t{i}", message_id=f"<{i}@x>",
                 subject=subject, sender_name=kw.pop("name", "Sam"), sender_addr=sender, to=kw.pop("to", [ME]),
                 date=kw.pop("date", NOW - timedelta(minutes=i)), body_text=body, **kw)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


# -- text -------------------------------------------------------------------
def test_strip_quoted_drops_gmail_quote_block_even_when_wrapped():
    text = "Thursday works.\n\nOn Mon, Sep 28, 2026 at 9:15 AM Jane Lee <j.lee@uni.edu>\nwrote:\n> Can you do Thursday?"
    assert strip_quoted(text) == "Thursday works."


def test_strip_quoted_drops_outlook_header_block():
    text = "See attached.\n\nFrom: Sam <sam@studio.co>\nSent: Monday\nTo: me\n\nold stuff"
    assert strip_quoted(text) == "See attached."


# -- rules ------------------------------------------------------------------
def test_codes_are_fyi_and_never_urgent():
    s = rules.pre_sort(mail(1, subject="Your verification code", body="Use 482913 to sign in."), {})
    assert (s.category, s.urgent, s.source) == ("fyi", False, "rule")


def test_code_words_without_a_code_go_to_the_model():
    assert rules.pre_sort(mail(1, subject="Question", body="What's the security code policy?"), {}) is None


def test_muted_sender_is_noise():
    assert rules.pre_sort(mail(1), {"sam@studio.co": "mute"}).category == "noise"


def test_vip_is_forced_urgent_and_at_least_read():
    s = rules.apply_vip(Sort("fyi", False), mail(1), {"sam@studio.co": "vip"})
    assert (s.category, s.urgent) == ("read", True)


def test_signals_describe_relationship_and_addressing():
    e = mail(1, cc=[], is_bulk=True)
    out = rules.signals(e, me={ME}, contacts={"sam@studio.co"}, sender_rules={},
                        thread=[mail(9, sender=ME)])
    assert "You have emailed this sender in the past year." in out
    assert "You have written in this conversation before." in out
    assert "Sent directly to you (you are in To)." in out
    assert "Has mailing-list / automated-sender headers." in out


# -- classify ---------------------------------------------------------------
def test_prompt_marks_your_own_thread_messages_and_includes_corrections():
    earlier = mail(9, sender=ME, body="Here's my rate.\n\nOn Mon someone wrote:\n> QUOTEDTEXT")
    item = classify.Item("gmail:1", mail(1), ["sig"], [earlier])
    correction = (mail(5, sender="news@shop.com", subject="Sale"), Sort("noise", False, source="you", model_category="read"))
    prompt, refs = classify.build_prompt([item], {ME}, [correction], NOW)
    assert refs == {"e1": item}
    assert "you] Here's my rate." in prompt and "QUOTEDTEXT" not in prompt
    assert 'news@shop.com, subject "Sale": noise (you had said read)' in prompt


def test_classify_maps_verdicts_and_drops_blurbs_for_fyi(monkeypatch):
    items = [classify.Item("gmail:1", mail(1)), classify.Item("gmail:2", mail(2))]

    def fake(prompt, **kw):
        return {"emails": [
            {"id": "e1", "category": "reply", "urgent": True, "summary": "Sign contract", "blurb": "From Maya's intro.", "reason": "r"},
            {"id": "e2", "category": "fyi", "urgent": False, "summary": "Receipt", "blurb": "should vanish", "reason": "r"},
            {"id": "e99", "category": "reply", "urgent": False, "summary": "ghost", "blurb": "", "reason": "r"},
        ]}
    monkeypatch.setattr(classify, "run_structured", fake)
    out = classify.classify(items, me={ME}, corrections=[], now=NOW)
    assert set(out) == {"gmail:1", "gmail:2"}
    assert (out["gmail:1"].category, out["gmail:1"].urgent, out["gmail:1"].blurb) == ("reply", True, "From Maya's intro.")
    assert out["gmail:2"].blurb == ""


# -- store ------------------------------------------------------------------
def test_correction_remembers_what_the_model_said(store):
    e = mail(1)
    store.save_emails([e])
    store.save_sort(key_of(e), Sort("read", False, source="model"))
    store.correct(key_of(e), "reply", True)
    again = store.correct(key_of(e), "noise", False)   # a second correction keeps the model's original
    assert (again.category, again.source, again.model_category) == ("noise", "you", "read")
    assert store.corrections()[0][0].id == "1"


# -- sorter -----------------------------------------------------------------
@pytest.fixture
def config():
    return Config(accounts=[Account("gmail", ME)])


def _fake_fetch(emails):
    return lambda config, since, limit=200: [AccountResult(Account("gmail", ME), emails)]


def test_run_sorts_once_and_never_overwrites_corrections(monkeypatch, store, config):
    emails = [mail(1), mail(2, subject="Your login code", body="Code: 123456")]
    monkeypatch.setattr(sorter, "fetch_all", _fake_fetch(emails))
    monkeypatch.setattr(sorter, "refresh_contacts", lambda *a, **k: None)
    calls = []

    def fake_classify(items, **kw):
        calls.append([i.key for i in items])
        return {i.key: Sort("read", False, "s", "", "r", "model") for i in items}
    monkeypatch.setattr(sorter, "classify", fake_classify)

    first = sorter.run(config, store, NOW - timedelta(days=1))
    assert calls == [["gmail:1"]]                       # the code email was settled by a rule
    assert first.sorted_now == 2
    assert {key_of(e): s.category for e, s in first.items} == {"gmail:1": "read", "gmail:2": "fyi"}

    store.correct("gmail:1", "reply", True)
    sorter.run(config, store, NOW - timedelta(days=1), resort=True)
    assert len(calls) == 1                              # the corrected email never went back to Claude
    assert store.sort("gmail:1").category == "reply"


def test_limit_reached_falls_back_and_retries_next_time(monkeypatch, store, config):
    monkeypatch.setattr(sorter, "fetch_all", _fake_fetch([mail(1)]))
    monkeypatch.setattr(sorter, "refresh_contacts", lambda *a, **k: None)

    def limited(items, **kw):
        raise LimitReached("usage limit reached")
    monkeypatch.setattr(sorter, "classify", limited)
    run = sorter.run(config, store, NOW - timedelta(days=1))
    assert run.limit_hit and store.sort("gmail:1").source == "fallback"

    monkeypatch.setattr(sorter, "classify", lambda items, **kw: {i.key: Sort("reply", False, source="model") for i in items})
    sorter.run(config, store, NOW - timedelta(days=1))
    assert store.sort("gmail:1").category == "reply"


# -- digest -----------------------------------------------------------------
def _items():
    return [
        (mail(1, name="Dr. Lee", subject="Review"), Sort("reply", False, "Thursday 2pm review?", "Your advisor.")),
        (mail(2, name="Chase", subject="Payment"), Sort("reply", True, "$240 due Oct 3", "")),
        (mail(3, name="Mom", subject="Pics"), Sort("read", False, "Photos from Sunday", "She mentioned the lake.")),
        (mail(4, name="UPS"), Sort("fyi", False, "Package out for delivery")),
        (mail(5), Sort("noise", False)), (mail(6), Sort("noise", False)),
        (mail(7), None),
    ]


def test_collapsed_digest_shows_only_needs_you_spaced_out_without_account_label():
    blocks = digest.render(digest.compose(_items(), NOW), html=False).split("\n\n")
    assert blocks[0].endswith("digest — 2 need you")
    assert blocks[1] == "🔴 1. Chase\n$240 due Oct 3"
    assert blocks[2] == "2. Dr. Lee\nThursday 2pm review?\n↳ Your advisor."
    assert len(blocks) == 3                                   # to read / FYI collapsed


def test_expanded_sections_have_one_line_each_and_no_context():
    text = digest.render(digest.compose(_items(), NOW), html=False, show_read=True, show_fyi=True)
    blocks = text.split("\n\n")
    assert blocks[3:] == ["📖 To read (1)", "3. Mom — Photos from Sunday",          # blank line between items
                          "📦 FYI (1)", "4. UPS — Package out for delivery", "🗑 2 noise hidden"]


def test_keyboard_toggles_each_section_independently():
    d = digest.compose(_items(), NOW)
    row = digest.keyboard(9, d)["inline_keyboard"][-1]
    assert [b["text"] for b in row] == ["📖 To read (1)", "📦 FYI (1)"]
    assert digest.parse_callback(row[0]["callback_data"]) == (9, True, False)
    row = digest.keyboard(9, d, show_read=True)["inline_keyboard"][-1]
    assert row[0]["text"] == "▾ Hide to read"
    assert digest.parse_callback(row[1]["callback_data"]) == (9, True, True)
    assert digest.parse_callback("evil:1:11") is None


def test_each_email_that_needs_you_gets_a_reply_button():
    first_row = digest.keyboard(9, digest.compose(_items(), NOW))["inline_keyboard"][0]
    assert [b["text"] for b in first_row] == ["✍️ 1", "✍️ 2"]
    assert digest.parse_reply_callback(first_row[1]["callback_data"]) == (9, 2)


def test_telegram_text_ends_with_a_spacer_line_before_the_buttons():
    text = digest.render(digest.compose(_items(), NOW))
    assert text.endswith("\n⠀")


def test_account_label_appears_with_two_accounts():
    other = Email(account="me@hotmail.com", provider="outlook", id="o1", thread_id="o1", message_id="",
                  subject="s", sender_name="Sam", sender_addr="sam@x.co", date=NOW)
    text = digest.render(digest.compose([(mail(1, name="Ann"), Sort("reply", False, "a")),
                                         (other, Sort("reply", False, "b"))], NOW), html=False)
    assert "Ann · Gmail" in text and "Sam · Outlook" in text


def test_digest_with_nothing_needing_you():
    assert "nothing needs you" in digest.build([(mail(1), Sort("fyi", False))], NOW)


def test_mit_mail_gets_its_own_label_next_to_gmail():
    mit = mail(8, name="Prof Lee", via="mit")
    text = digest.render(digest.compose([(mail(1, name="Ann"), Sort("reply", False, "a")),
                                         (mit, Sort("reply", False, "b"))], NOW), html=False)
    assert "Ann · Gmail" in text and "Prof Lee · MIT" in text


def test_only_mit_mail_needs_no_label():
    text = digest.render(digest.compose([(mail(8, name="Prof Lee", via="mit"), Sort("reply", False, "b"))], NOW), html=False)
    assert "Prof Lee\n" in text


def test_priorities_profile_goes_into_the_prompt_ahead_of_the_emails():
    prompt, _ = classify.build_prompt([classify.Item("gmail:1", mail(1))], {ME}, [], NOW, "- ASA funding is always reply")
    assert prompt.index("ASA funding is always reply") < prompt.index("Sort these 1 emails")


def test_done_items_in_to_read_are_struck_and_not_counted():
    d = digest.compose(_items(), NOW)
    d.reads[0].done = True
    assert "📖 To read (0)" in str(digest.keyboard(9, d))
    assert "✓ <s>3." in digest.render(d, show_read=True)
