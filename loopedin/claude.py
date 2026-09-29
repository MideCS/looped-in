"""Run Claude through the local Claude Code install (`claude -p`), on your Claude plan.

Same shape as papyrus-notes' tools/tickets/agent_runner.py: the prompt goes in
on stdin (Windows caps command lines at ~32k characters), no tools, no saved
session, and a timeout that kills the whole process tree rather than just the
top process.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import data_dir


class ClaudeError(Exception):
    pass


class LimitReached(ClaudeError):
    """Your plan's usage limit is used up; callers fall back to rules until it resets."""


_LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "quota", "out of extra usage", "5-hour limit")


def claude_path() -> str:
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    if fallback.exists():
        return str(fallback)
    raise ClaudeError("Claude Code isn't on PATH. Install it or add ~/.local/bin to PATH.")


def build_command(schema: dict, system: str, model: str, effort: str) -> list[str]:
    return [
        claude_path(), "-p",
        "--model", model,
        "--effort", effort,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]


def run_structured(prompt: str, *, schema: dict, system: str, model: str = "haiku", effort: str = "low",
                   timeout: int = 240) -> dict:
    env = dict(os.environ)
    # Without this, an API key in the environment would silently bill that key instead of your plan.
    env.pop("ANTHROPIC_API_KEY", None)
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        build_command(schema, system, model, effort), cwd=data_dir(), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    try:
        stdout, stderr = process.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        raise ClaudeError(f"Claude took longer than {timeout}s and was stopped.")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout).strip()[:300]
        if any(w in detail.lower() for w in _LIMIT_WORDS):
            raise LimitReached(detail)
        raise ClaudeError(f"Claude exited {process.returncode} without JSON: {detail}")

    if payload.get("is_error") or payload.get("subtype") != "success":
        detail = str(payload.get("result") or payload.get("subtype"))[:300]
        if any(w in detail.lower() for w in _LIMIT_WORDS):
            raise LimitReached(detail)
        raise ClaudeError(f"Claude reported an error: {detail}")
    output = payload.get("structured_output")
    if not isinstance(output, dict):
        raise ClaudeError("Claude returned no structured output.")
    return output


def _kill_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass
