import subprocess

import pytest

from loopedin import selfedit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (tmp_path / "a.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "start")
    monkeypatch.setattr(selfedit, "REPO", tmp_path)
    return tmp_path


def fake_claude(repo, text="x = 2\n"):
    def edit(request):
        (repo / "a.py").write_text(text)
        (repo / "new.py").write_text("y = 1\n")
        return "Set x to 2."
    return edit


def test_passing_change_is_committed_and_can_be_undone(repo, monkeypatch):
    monkeypatch.setattr(selfedit, "run_claude", fake_claude(repo))
    monkeypatch.setattr(selfedit, "tests_pass", lambda: (True, "3 passed"))
    outcome = selfedit.change("make x 2")
    assert outcome.ok and outcome.commit and "a.py" in outcome.files
    assert selfedit.MARK in selfedit.git("log", "-1", "--format=%B")
    assert selfedit.git("status", "--porcelain") == ""

    assert selfedit.undo(outcome.commit) == "Undone."
    assert (repo / "a.py").read_text() == "x = 1\n" and not (repo / "new.py").exists()


def test_failing_tests_throw_the_change_away(repo, monkeypatch):
    monkeypatch.setattr(selfedit, "run_claude", fake_claude(repo))
    monkeypatch.setattr(selfedit, "tests_pass", lambda: (False, "1 failed"))
    outcome = selfedit.change("make x 2")
    assert not outcome.ok and "1 failed" in outcome.message
    assert (repo / "a.py").read_text() == "x = 1\n" and not (repo / "new.py").exists()


def test_uncommitted_work_on_the_laptop_is_left_alone(repo, monkeypatch):
    (repo / "a.py").write_text("mine\n")
    monkeypatch.setattr(selfedit, "run_claude", lambda r: pytest.fail("shouldn't run"))
    assert not selfedit.change("anything").ok
    assert (repo / "a.py").read_text() == "mine\n"


def test_undo_only_reverts_changes_made_from_telegram(repo):
    assert "isn't a change made from Telegram" in selfedit.undo("HEAD")


def test_commit_title_is_the_first_sentence_cut_at_a_word():
    assert selfedit.title("I added /ping. It replies pong.") == "I added /ping"
    long = selfedit.title("I added a /ping command that replies pong, listed it in the help message and a test")
    assert len(long) <= 72 and long.endswith("…") and " messa…" not in long
