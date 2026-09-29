"""Code changes asked for from Telegram ("/tune" requests that settings can't cover).

Claude Code edits this repo on the laptop, with file tools and pytest only (no other shell
commands, no email data). The bot then runs the tests itself: if they pass, the change is committed
(not pushed: the repo is public, so you review before anything leaves the laptop) and the bot
restarts on the new code; if they fail, every edit is thrown away. Undo reverts the commit.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .claude import ClaudeError, _kill_tree, claude_path
from .config import data_dir

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
# How the editing Claude runs the tests: its shell is bash, so a repo-relative path with forward
# slashes, which is also what its permission rule has to match.
try:
    TEST_PYTHON = Path(PYTHON).resolve().relative_to(REPO).as_posix()
except ValueError:
    TEST_PYTHON = Path(PYTHON).as_posix()
TIMEOUT = 20 * 60
MARK = "Changed from Telegram"   # every commit made here says so, and Undo only reverts those

PROMPT = """You are changing the code of "Looped In", the Python Telegram email bot in this repository, \
because its owner asked for it from their phone. Their request:

<request>
{request}
</request>

Rules:
- Read the relevant code first and match its style: small, plain changes; comments only where the \
code isn't obvious.
- The bot must never send email; it only saves drafts.
- This repo is public. Never write personal data into it: no real names, email addresses, email \
content, tokens or keys. Mail data and secrets live outside the repo and stay there.
- Add or update tests in tests/ for what you change, and run them with: \
{python} -m pytest -q -p no:cacheprovider --basetemp "{basetemp}"
- Don't commit, and don't touch git; the bot commits once the tests pass.
- If the request is unclear, risky, or can't be done, change nothing and say why.

Finish with 1-3 short plain sentences for a phone screen saying what you changed (or why you didn't). \
No markdown."""


@dataclass
class Outcome:
    ok: bool
    message: str
    commit: str = ""
    files: str = ""


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise ClaudeError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:200]}")
    return result.stdout.strip()


def tests_pass() -> tuple[bool, str]:
    basetemp = data_dir() / "pytest"
    result = subprocess.run([PYTHON, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp)],
                            cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=600)
    lines = result.stdout.strip().splitlines()
    return result.returncode == 0, lines[-1] if lines else result.stderr.strip()[-200:]


def discard() -> None:
    """Throw away every edit since the last commit. Only called when the tree was clean before."""
    git("reset", "--hard", "HEAD")
    git("clean", "-fd")


def run_claude(request: str) -> str:
    prompt = PROMPT.format(request=request, python=TEST_PYTHON, basetemp=(data_dir() / "pytest").as_posix())
    command = [claude_path(), "-p", "--output-format", "json", "--no-session-persistence",
               "--strict-mcp-config", "--permission-mode", "acceptEdits",
               "--allowedTools", f"Read,Edit,Write,Glob,Grep,Bash({TEST_PYTHON} -m pytest:*)"]
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)   # bill the plan, not a stray key
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(command, cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kwargs)
    try:
        stdout, stderr = process.communicate(prompt, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        raise ClaudeError(f"Claude took longer than {TIMEOUT // 60} minutes and was stopped.")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise ClaudeError(f"Claude exited {process.returncode}: {(stderr or stdout).strip()[:300]}")
    if payload.get("is_error"):
        raise ClaudeError(str(payload.get("result"))[:300])
    return str(payload.get("result") or "").strip()


def change(request: str) -> Outcome:
    if git("status", "--porcelain"):
        return Outcome(False, "The code on the laptop has changes that aren't committed, so I left it alone. "
                              "Commit or discard them first.")
    try:
        summary = run_claude(request)
    except ClaudeError:
        discard()
        raise
    files = git("status", "--porcelain")
    if not files:
        return Outcome(False, summary or "Claude didn't change anything.")
    ok, detail = tests_pass()
    if not ok:
        discard()
        return Outcome(False, f"{summary}\n\nThe tests failed ({detail}), so I threw the change away.")
    git("add", "-A")
    names = ", ".join(git("diff", "--cached", "--name-only").splitlines())
    first_line = title(summary)
    git("commit", "-q", "-m", f"{first_line}\n\n{MARK}.\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    return Outcome(True, summary, git("rev-parse", "--short", "HEAD"), names)


def title(summary: str) -> str:
    """A commit title from Claude's summary: its first sentence, cut at a word if it's long."""
    first = (summary.strip().splitlines() or ["Change from Telegram"])[0].split(". ")[0].rstrip(".")
    if len(first) > 72:
        first = first[:70].rsplit(" ", 1)[0] + "…"
    return first


def undo(commit: str) -> str:
    """Revert a commit made from Telegram. Returns what happened, for the chat."""
    if git("status", "--porcelain"):
        return "The code on the laptop has uncommitted changes, so I didn't undo anything."
    if MARK not in git("log", "-1", "--format=%B", commit):
        return "That isn't a change made from Telegram, so I left it alone."
    git("revert", "--no-edit", commit)
    return "Undone."
