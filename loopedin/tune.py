"""Change how the bot behaves from Telegram: "/tune keep answers to two lines", "stop flagging Piazza".

One request can touch three things, and Claude picks which:
- bot rules: standing instructions for how the assistant answers you (added to its prompt),
- priorities.md: what counts as important when email is sorted,
- draft rules: how replies are written (the same list "style: <rule>" adds to).
The previous versions are kept so one tap undoes the change. Anything that needs a code change is
said plainly instead of pretended.
"""

import json
from dataclasses import dataclass

from .claude import run_structured
from .sorter import load_priorities
from .config import data_dir
from .store import Store

SYSTEM = """You adjust the settings of "Looped In", a personal Telegram bot that sorts someone's email \
(Gmail plus forwarded MIT mail), sends digests at 8am, 1pm and 6pm, answers questions about their \
email, and writes reply drafts in their style. The person tells you how they want it to behave \
differently. You can change exactly three settings:

1. bot_rules: short standing instructions for the assistant that answers their messages in \
Telegram (tone, length, format, what to mention). Example: "Keep answers to 2 lines unless I ask \
for detail."
2. priorities: the markdown profile used when sorting email into needs-you / to read / FYI / \
noise (who and what matters, what to ignore). Edit it in place, keeping its structure; return it \
whole.
3. draft_rules: rules every reply draft follows. Example: "Never sign off with Best,".

Return the full new list or text for each setting you change, and set its `changed` flag; leave the \
others unchanged (changed=false, and repeat them as given). Change only what the request is about; \
keep every other rule. Remove or rewrite rules the request contradicts.

Some things need a code change and can't be done with these settings (digest times, button layout, \
new features, how the digest message is formatted). Put a one-line explanation in `needs_code` and \
change nothing for that part. If the request is unclear, change nothing and ask in `summary`.

`summary`: one or two short lines to the person saying what changed, in plain words."""

SCHEMA = {
    "type": "object",
    "properties": {
        "bot_rules": {"type": "array", "items": {"type": "string"}},
        "bot_rules_changed": {"type": "boolean"},
        "priorities": {"type": "string"},
        "priorities_changed": {"type": "boolean"},
        "draft_rules": {"type": "array", "items": {"type": "string"}},
        "draft_rules_changed": {"type": "boolean"},
        "needs_code": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["bot_rules", "bot_rules_changed", "priorities", "priorities_changed", "draft_rules",
                 "draft_rules_changed", "needs_code", "summary"],
}


@dataclass
class Settings:
    bot_rules: list[str]
    priorities: str
    draft_rules: list[str]


@dataclass
class Change:
    summary: str
    needs_code: str
    changed: list[str]     # which settings changed, for the Undo button


def bot_rules(store: Store) -> list[str]:
    return json.loads(store.get_meta("bot_rules") or "[]")


def current(store: Store) -> Settings:
    # notes() is newest first; keep the order they were added in.
    return Settings(bot_rules(store), load_priorities(), list(reversed(store.notes("rule"))))


def save(store: Store, s: Settings) -> None:
    store.set_meta("bot_rules", json.dumps(s.bot_rules))
    (data_dir() / "priorities.md").write_text(s.priorities, encoding="utf-8")
    store.replace_notes("rule", s.draft_rules)


def build_prompt(request: str, s: Settings) -> str:
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs) or "(none)"
    return (f"Current bot_rules:\n{bullets(s.bot_rules)}\n\n"
            f"Current draft_rules:\n{bullets(s.draft_rules)}\n\n"
            f"Current priorities:\n<priorities>\n{s.priorities or '(empty)'}\n</priorities>\n\n"
            f"The person's request:\n{request}")


def apply(store: Store, request: str) -> Change:
    before = current(store)
    out = run_structured(build_prompt(request, before), schema=SCHEMA, system=SYSTEM,
                         model="sonnet", effort="low", timeout=240)
    after = Settings(before.bot_rules, before.priorities, before.draft_rules)
    changed = []
    if out["bot_rules_changed"]:
        after.bot_rules = [r.strip() for r in out["bot_rules"] if r.strip()]
        changed.append("how I answer you")
    if out["priorities_changed"] and out["priorities"].strip():
        after.priorities = out["priorities"].strip() + "\n"
        changed.append("what counts as important")
    if out["draft_rules_changed"]:
        after.draft_rules = [r.strip() for r in out["draft_rules"] if r.strip()]
        changed.append("how drafts are written")
    if changed:
        store.set_meta("tune_undo", json.dumps(before.__dict__))
        save(store, after)
    return Change(out["summary"].strip(), out["needs_code"].strip(), changed)


def undo(store: Store) -> bool:
    raw = store.get_meta("tune_undo")
    if not raw:
        return False
    save(store, Settings(**json.loads(raw)))
    store.set_meta("tune_undo", "")
    return True


def describe(store: Store) -> str:
    """Plain text for /tune with nothing after it."""
    s = current(store)
    bullets = lambda xs, empty: "\n".join(f"• {x}" for x in xs) if xs else empty
    return "\n\n".join([
        "How I answer you:\n" + bullets(s.bot_rules, "• nothing special yet"),
        "How drafts are written:\n" + bullets(s.draft_rules, "• no rules yet"),
        "What counts as important: priorities.md" + (" is set up" if s.priorities.strip() else " is empty"),
        "Change any of it by saying what you want, e.g.\n/tune keep answers to two lines\n"
        "/tune Piazza digests are never important\n/tune sign off drafts with just my name",
    ])
