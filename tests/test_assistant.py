from datetime import datetime, timedelta, timezone

import pytest

from loopedin import assistant
from loopedin.models import Email
from loopedin.store import Sort, Store

ME = "me@gmail.com"


def mail(i, name, subject, body, minutes_ago=10) -> Email:
    return Email(account=ME, provider="gmail", id=str(i), thread_id=str(i), message_id=f"<{i}@x>", subject=subject,
                 sender_name=name, sender_addr=f"{name.split()[0].lower()}@x.com", to=[ME],
                 date=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), body_text=body)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    emails = [
        mail(1, "Sam Rivera", "Want to help shape the future of AI?", "Can you send your reference's contact?", 30),
        mail(2, "E14 Fund", "ScienceClaw Hack: Open for applications",
             "Join us at the MIT Media Lab Oct 30 - Nov 1 to build AI agents for science. Apply by Oct 15.", 20),
        mail(3, "Google", "Security alert", "App password created", 10),
    ]
    s.save_emails(emails)
    s.save_sort("gmail:1", Sort("reply", True, "Reference needed"))
    s.save_sort("gmail:2", Sort("fyi", False, "Hackathon applications open"))
    s.save_sort("gmail:3", Sort("reply", True, "Security alert"))
    s.record_digest({"gmail:1": 2, "gmail:3": 1, "gmail:2": 5}, [])
    s.dismiss("gmail:3")
    yield s
    s.close()


def test_index_tags_numbers_categories_and_done(store):
    text = assistant.render_index(assistant.build_index(store, {ME}))
    assert "[#1, reply, urgent, done]" in text and "| Google |" in text
    assert "[#5, fyi]" in text and "E14 Fund" in text


def test_best_match_finds_the_hackathon_from_loose_wording(store):
    index = assistant.build_index(store, {ME})
    top = assistant.best_matches(index, "Tell me about the E14 hack")[0]
    assert top.email.sender_name == "E14 Fund"


def test_digest_number_in_question_boosts_that_email(store):
    index = assistant.build_index(store, {ME})
    assert assistant.best_matches(index, "what's #2 about")[0].email.sender_name == "Sam Rivera"


def test_prompt_includes_full_text_of_matches_only(store):
    prompt = assistant.build_prompt("Tell me about the E14 hack", assistant.build_index(store, {ME}), [])
    assert "Apply by Oct 15" in prompt
    assert "App password created" not in prompt       # not a match, so only its index line appears


def test_ask_maps_the_action_to_an_email_and_remembers_the_turn(store, monkeypatch):
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"] = prompt
        ref = next(line.split()[0] for line in prompt.splitlines() if "Sam Rivera" in line and line.startswith("E"))
        return {"answer": "Drafting that now.", "action": "reply", "emails": [ref], "notes": "Jane is my reference"}
    monkeypatch.setattr(assistant, "run_structured", fake)

    r = assistant.ask(store, "reply to sam saying jane is my reference", {ME})
    assert (r.action, r.key, r.notes) == ("reply", "gmail:1", "Jane is my reference")
    assert assistant.load_history(store)[-1]["user"] == "reply to sam saying jane is my reference"


def test_unknown_email_ref_downgrades_to_no_action(store, monkeypatch):
    monkeypatch.setattr(assistant, "run_structured",
                        lambda prompt, **kw: {"answer": "ok", "action": "dismiss", "emails": ["E999"], "notes": ""})
    r = assistant.ask(store, "dismiss everything", {ME})
    assert r.action == "none" and r.key is None



def test_follow_ups_keep_the_full_text_of_the_email_being_discussed(store):
    index = assistant.build_index(store, {ME})
    e14 = assistant.best_matches(index, "Tell me about the E14 hack")[0]
    history = [{"user": "Tell me about the E14 hack", "assistant": "It's Oct 30.", "emails": [assistant.key_of(e14.email)]}]
    prompt = assistant.build_prompt("Do you only have a short summary?", index, history)
    assert f'<email id="{e14.ref}"' in prompt



def test_dismiss_can_take_several_emails(store, monkeypatch):
    monkeypatch.setattr(assistant, "run_structured", lambda prompt, **kw: {
        "answer": "", "action": "dismiss", "emails": ["E1", "E2"], "notes": "", "about": []})
    r = assistant.ask(store, "dismiss both", {ME})
    assert r.action == "dismiss" and len(r.keys) == 2



def test_mit_mail_is_tagged_in_the_index(store):
    index = assistant.build_index(store, {ME})
    index[0].email.via = "mit"
    assert "MIT]" in assistant.render_index(index).splitlines()[0]
