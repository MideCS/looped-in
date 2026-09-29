import json

import pytest

from loopedin import assistant, tune
from loopedin.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPEDIN_HOME", str(tmp_path))
    (tmp_path / "priorities.md").write_text("# Priorities\n- Job processes matter\n", encoding="utf-8")
    s = Store(tmp_path / "t.db")
    s.add_note("rule", "Never sign off with Best,")
    s.add_note("rule", "Keep it short")
    yield s
    s.close()


def answer(**changes):
    out = {"bot_rules": [], "bot_rules_changed": False, "priorities": "", "priorities_changed": False,
           "draft_rules": [], "draft_rules_changed": False, "needs_code": "", "summary": "Done"}
    return out | changes


def test_prompt_shows_every_current_setting(store):
    prompt = tune.build_prompt("be brief", tune.current(store))
    assert "- Never sign off with Best,\n- Keep it short" in prompt
    assert "Job processes matter" in prompt and prompt.endswith("be brief")


def test_changes_only_what_claude_marks_changed_and_undo_restores(store, tmp_path, monkeypatch):
    monkeypatch.setattr(tune, "run_structured", lambda prompt, **kw: answer(
        bot_rules=["Two lines max"], bot_rules_changed=True, draft_rules=["ignored"],
        priorities="# Priorities\n- Piazza is noise", priorities_changed=True))
    change = tune.apply(store, "two lines max, and Piazza is noise")
    assert change.changed == ["how I answer you", "what counts as important"]
    assert tune.bot_rules(store) == ["Two lines max"]
    assert "Piazza is noise" in (tmp_path / "priorities.md").read_text(encoding="utf-8")
    assert tune.current(store).draft_rules == ["Never sign off with Best,", "Keep it short"]

    assert tune.undo(store)
    assert tune.bot_rules(store) == []
    assert "Job processes matter" in (tmp_path / "priorities.md").read_text(encoding="utf-8")
    assert not tune.undo(store)


def test_draft_rules_are_replaced_in_order(store, monkeypatch):
    monkeypatch.setattr(tune, "run_structured", lambda prompt, **kw: answer(
        draft_rules=["Keep it short", "Sign off with just Mide"], draft_rules_changed=True))
    tune.apply(store, "sign off with just my name")
    assert tune.current(store).draft_rules == ["Keep it short", "Sign off with just Mide"]


def test_nothing_changed_leaves_no_undo(store, monkeypatch):
    monkeypatch.setattr(tune, "run_structured", lambda prompt, **kw: answer(needs_code="Digest times are fixed"))
    change = tune.apply(store, "send digests at 9")
    assert change.changed == [] and change.needs_code
    assert store.get_meta("tune_undo") is None


def test_assistant_follows_bot_rules_and_can_hand_off_to_tune(store, monkeypatch):
    store.set_meta("bot_rules", json.dumps(["Two lines max"]))
    seen = {}

    def fake(prompt, **kw):
        seen["system"] = kw["system"]
        return {"answer": "", "action": "tune", "emails": [], "notes": "never flag Piazza", "about": []}

    monkeypatch.setattr(assistant, "run_structured", fake)
    result = assistant.ask(store, "from now on never flag Piazza", {"me@x.com"})
    assert "- Two lines max" in seen["system"]
    assert result.action == "tune" and result.notes == "never flag Piazza"
