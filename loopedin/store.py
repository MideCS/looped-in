"""SQLite state in %LOCALAPPDATA%\\looped-in\\loopedin.db.

Emails are cached so each one is sorted exactly once, and so the digest and
the reply flow can refer back to them without refetching. A sort the user
corrected is marked source='you' and is never overwritten by the model.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir
from .models import Email

CHAT_DAYS = 30   # how long the chat transcript is kept

CATEGORIES = ("reply", "read", "fyi", "noise")

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    key TEXT PRIMARY KEY,           -- provider:id
    account TEXT NOT NULL,
    date TEXT,
    data TEXT NOT NULL              -- the Email as JSON
);
CREATE INDEX IF NOT EXISTS emails_date ON emails(date);

CREATE TABLE IF NOT EXISTS sorts (
    key TEXT PRIMARY KEY REFERENCES emails(key),
    category TEXT NOT NULL,
    urgent INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    blurb TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,           -- rule | model | fallback | you
    model_category TEXT,            -- what the model said before you corrected it
    sorted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS senders (
    address TEXT PRIMARY KEY,
    rule TEXT NOT NULL CHECK (rule IN ('vip', 'mute'))
);

CREATE TABLE IF NOT EXISTS contacts (
    address TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digested (
    key TEXT PRIMARY KEY,           -- each email appears in exactly one digest
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    number INTEGER                  -- its number in that digest; NULL for FYI/noise counts
);

CREATE TABLE IF NOT EXISTS dismissed (
    key TEXT PRIMARY KEY,           -- you said "done": no reply needed, keep it out of digests
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerted (
    key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,              -- the email being replied to
    instruction TEXT NOT NULL,      -- what you told the bot to say
    body TEXT NOT NULL,
    gmail_message_id TEXT,          -- Message-ID of the draft saved in Gmail
    link TEXT,
    status TEXT NOT NULL,           -- open | replaced | skipped
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- rule (you set it) | feedback (a change you asked for on a draft)
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat (       -- transcript of the Telegram chat, kept on this laptop only
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    who TEXT NOT NULL,              -- you | you (tapped) | bot | bot (edited)
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (     -- meetings found in email and offered for your calendar
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_key TEXT NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    who TEXT NOT NULL,
    status TEXT NOT NULL,           -- offered | skipped
    created_at TEXT NOT NULL,
    UNIQUE (start, who)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


@dataclass
class Sort:
    category: str
    urgent: bool
    summary: str = ""
    blurb: str = ""
    reason: str = ""
    source: str = "model"
    model_category: str | None = None


def key_of(e: Email) -> str:
    return f"{e.provider}:{e.id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def email_to_json(e: Email) -> str:
    data = asdict(e)
    data["date"] = e.date.isoformat() if e.date else None
    return json.dumps(data)


def email_from_json(text: str) -> Email:
    data = json.loads(text)
    data["date"] = datetime.fromisoformat(data["date"]) if data.get("date") else None
    return Email(**data)


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "loopedin.db"
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(digests)")}
        if "message_id" not in columns:   # the Telegram message, so the digest can be edited later
            with self.db:
                self.db.execute("ALTER TABLE digests ADD COLUMN message_id INTEGER")

    def close(self) -> None:
        self.db.close()

    # -- emails ------------------------------------------------------------
    def save_emails(self, emails: list[Email]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO emails(key, account, date, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data = excluded.data",
                [(key_of(e), e.account, e.date.isoformat() if e.date else None, email_to_json(e)) for e in emails],
            )

    def emails_since(self, since: datetime, accounts: set[str] | None = None) -> list[Email]:
        rows = self.db.execute("SELECT data FROM emails WHERE date >= ? ORDER BY date DESC",
                               (since.astimezone(timezone.utc).isoformat(),)).fetchall()
        emails = [email_from_json(r["data"]) for r in rows]
        return [e for e in emails if accounts is None or e.account in accounts]

    def email(self, key: str) -> Email | None:
        row = self.db.execute("SELECT data FROM emails WHERE key = ?", (key,)).fetchone()
        return email_from_json(row["data"]) if row else None

    # -- sorts -------------------------------------------------------------
    def sort(self, key: str) -> Sort | None:
        row = self.db.execute("SELECT * FROM sorts WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return Sort(row["category"], bool(row["urgent"]), row["summary"], row["blurb"],
                    row["reason"], row["source"], row["model_category"])

    def sorts(self, keys: list[str]) -> dict[str, Sort]:
        return {k: s for k in keys if (s := self.sort(k))}

    def save_sort(self, key: str, s: Sort) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sorts(key, category, urgent, summary, blurb, reason, source, model_category, sorted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "category = excluded.category, urgent = excluded.urgent, summary = excluded.summary, "
                "blurb = excluded.blurb, reason = excluded.reason, source = excluded.source, "
                "model_category = excluded.model_category, sorted_at = excluded.sorted_at",
                (key, s.category, int(s.urgent), s.summary, s.blurb, s.reason, s.source, s.model_category, _now()),
            )

    def correct(self, key: str, category: str, urgent: bool) -> Sort:
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        current = self.sort(key)
        if current is None:
            raise KeyError(key)
        before = current.model_category if current.source == "you" else current.category
        corrected = Sort(category, urgent, current.summary, current.blurb, current.reason, "you", before)
        self.save_sort(key, corrected)
        return corrected

    def corrections(self, limit: int = 12) -> list[tuple[Email, Sort]]:
        """Your most recent corrections, used to teach the model your preferences."""
        rows = self.db.execute(
            "SELECT e.data, s.* FROM sorts s JOIN emails e ON e.key = s.key "
            "WHERE s.source = 'you' ORDER BY s.sorted_at DESC LIMIT ?", (limit,)).fetchall()
        return [(email_from_json(r["data"]), Sort(r["category"], bool(r["urgent"]), r["summary"], r["blurb"],
                                                   r["reason"], r["source"], r["model_category"])) for r in rows]

    # -- digests & alerts --------------------------------------------------
    def undigested(self, since: datetime, accounts: set[str]) -> list[tuple[Email, "Sort"]]:
        """Sorted emails since `since` that haven't been in a digest yet."""
        rows = self.db.execute(
            "SELECT e.data, s.* FROM emails e JOIN sorts s ON s.key = e.key "
            "LEFT JOIN digested d ON d.key = e.key LEFT JOIN dismissed x ON x.key = e.key "
            "WHERE d.key IS NULL AND x.key IS NULL AND e.date >= ? ORDER BY e.date DESC",
            (since.astimezone(timezone.utc).isoformat(),)).fetchall()
        out = []
        for r in rows:
            e = email_from_json(r["data"])
            if e.account in accounts:
                out.append((e, Sort(r["category"], bool(r["urgent"]), r["summary"], r["blurb"],
                                    r["reason"], r["source"], r["model_category"])))
        return out

    def open_items(self, since: datetime, accounts: set[str]) -> list[tuple[Email, "Sort"]]:
        """Every sorted email since `since` you haven't dismissed, whether or not a digest showed it."""
        rows = self.db.execute(
            "SELECT e.data, s.* FROM emails e JOIN sorts s ON s.key = e.key "
            "LEFT JOIN dismissed x ON x.key = e.key "
            "WHERE x.key IS NULL AND e.date >= ? ORDER BY e.date DESC",
            (since.astimezone(timezone.utc).isoformat(),)).fetchall()
        out = []
        for r in rows:
            e = email_from_json(r["data"])
            if e.account in accounts:
                out.append((e, Sort(r["category"], bool(r["urgent"]), r["summary"], r["blurb"],
                                    r["reason"], r["source"], r["model_category"])))
        return out

    def record_digest(self, numbered: dict[str, int], counted: list[str]) -> int:
        with self.db:
            cur = self.db.execute("INSERT INTO digests(sent_at) VALUES (?)", (_now(),))
            digest_id = cur.lastrowid
            self.db.executemany("INSERT OR REPLACE INTO digested(key, digest_id, number) VALUES (?, ?, ?)",
                                [(k, digest_id, n) for k, n in numbered.items()] +
                                [(k, digest_id, None) for k in counted])
        return digest_id

    def set_digest_message(self, digest_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE digests SET message_id = ? WHERE id = ?", (message_id, digest_id))

    def digest_holding(self, key: str) -> tuple[int, int | None] | None:
        """(digest id, Telegram message id) of the digest an email appeared in."""
        row = self.db.execute("SELECT g.id, g.message_id FROM digested d JOIN digests g ON g.id = d.digest_id "
                              "WHERE d.key = ?", (key,)).fetchone()
        return (row["id"], row["message_id"]) if row else None

    def dismiss(self, key: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO dismissed(key, at) VALUES (?, ?)", (key, _now()))

    def dismissed(self, keys: list[str]) -> set[str]:
        return {k for k in keys if self.db.execute("SELECT 1 FROM dismissed WHERE key = ?", (k,)).fetchone()}

    def open_drafts(self, key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM drafts WHERE key = ? AND status = 'open'", (key,)).fetchall()

    def delete_digest(self, digest_id: int) -> None:
        """Undo record_digest when sending failed, so those emails make the next digest."""
        with self.db:
            self.db.execute("DELETE FROM digested WHERE digest_id = ?", (digest_id,))
            self.db.execute("DELETE FROM digests WHERE id = ?", (digest_id,))

    def digest_contents(self, digest_id: int) -> tuple[list[tuple[int, Email, "Sort"]], int, datetime] | None:
        """(numbered entries, noise count, sent_at) for a sent digest, with each email's current sort."""
        head = self.db.execute("SELECT sent_at FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if not head:
            return None
        rows = self.db.execute(
            "SELECT d.number, e.data, s.* FROM digested d JOIN emails e ON e.key = d.key "
            "JOIN sorts s ON s.key = d.key WHERE d.digest_id = ?", (digest_id,)).fetchall()
        entries, noise = [], 0
        for r in rows:
            if r["number"] is None:
                noise += r["category"] == "noise"
                continue
            entries.append((r["number"], email_from_json(r["data"]),
                            Sort(r["category"], bool(r["urgent"]), r["summary"], r["blurb"],
                                 r["reason"], r["source"], r["model_category"])))
        return entries, noise, datetime.fromisoformat(head["sent_at"])

    def last_digest_at(self) -> datetime | None:
        row = self.db.execute("SELECT sent_at FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row else None

    def digest_numbers(self, digest_id: int | None = None) -> dict[int, str]:
        """Number -> email key for a digest (the latest by default)."""
        if digest_id is None:
            row = self.db.execute("SELECT id FROM digests ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return {}
            digest_id = row["id"]
        rows = self.db.execute("SELECT key, number FROM digested WHERE digest_id = ? AND number IS NOT NULL",
                               (digest_id,)).fetchall()
        return {r["number"]: r["key"] for r in rows}

    def was_alerted(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM alerted WHERE key = ?", (key,)).fetchone() is not None

    def mark_alerted(self, key: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO alerted(key, sent_at) VALUES (?, ?)", (key, _now()))

    # -- drafts & style notes -----------------------------------------------
    def add_draft(self, key: str, instruction: str, body: str, gmail_message_id: str, link: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO drafts(key, instruction, body, gmail_message_id, link, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'open', ?)", (key, instruction, body, gmail_message_id, link, _now()))
        return cur.lastrowid

    def draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    def set_draft_status(self, draft_id: int, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE drafts SET status = ? WHERE id = ?", (status, draft_id))

    def add_note(self, kind: str, text: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO notes(kind, text, created_at) VALUES (?, ?, ?)", (kind, text.strip(), _now()))

    def digest_message(self, digest_id: int) -> int | None:
        row = self.db.execute("SELECT message_id FROM digests WHERE id = ?", (digest_id,)).fetchone()
        return row["message_id"] if row else None

    def add_event(self, email_key: str, title: str, start: datetime, who: str) -> int | None:
        """Record a meeting offered to you. None if it was already offered (same time, same person)."""
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO events(email_key, title, start, who, status, created_at) "
                "VALUES (?, ?, ?, ?, 'offered', ?)",
                (email_key, title, start.isoformat(), who.strip().lower(), _now()))
        return cur.lastrowid if cur.rowcount else None

    def event(self, event_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    def set_event_status(self, event_id: int, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))

    def add_chat(self, who: str, text: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO chat(at, who, text) VALUES (?, ?, ?)", (_now(), who, text))
            self.db.execute("DELETE FROM chat WHERE at < datetime('now', ?)", (f"-{CHAT_DAYS} days",))

    def chat(self, limit: int = 60) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT at, who, text FROM chat ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    def notes(self, kind: str, limit: int = 50) -> list[str]:
        rows = self.db.execute("SELECT text FROM notes WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))
        return [r["text"] for r in rows]

    def delete_meta(self, k: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM meta WHERE k = ?", (k,))

    # -- senders & contacts ------------------------------------------------
    def sender_rules(self) -> dict[str, str]:
        return {r["address"]: r["rule"] for r in self.db.execute("SELECT * FROM senders")}

    def set_sender_rule(self, address: str, rule: str | None) -> None:
        with self.db:
            if rule is None:
                self.db.execute("DELETE FROM senders WHERE address = ?", (address.lower(),))
            else:
                self.db.execute("INSERT INTO senders(address, rule) VALUES (?, ?) "
                                "ON CONFLICT(address) DO UPDATE SET rule = excluded.rule", (address.lower(), rule))

    def contacts(self) -> set[str]:
        return {r["address"] for r in self.db.execute("SELECT address FROM contacts")}

    def replace_contacts(self, addresses: set[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM contacts")
            self.db.executemany("INSERT INTO contacts(address) VALUES (?)", [(a,) for a in sorted(addresses)])
            self.set_meta("contacts_refreshed_at", _now())

    def get_meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
