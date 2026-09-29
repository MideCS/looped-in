"""Gmail over IMAP with an app password.

Mailboxes are always opened read-only and bodies fetched with BODY.PEEK, so
nothing here ever marks mail as read -- the bot only changes your mailbox when
you tell it to.
"""

import email
import imaplib
import re
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses

from .models import Email
from .secrets import get_secret
from .text import html_to_text, tidy

HOST = "imap.gmail.com"
_META = re.compile(rb"X-GM-THRID (\d+)|X-GM-MSGID (\d+)|FLAGS \(([^)]*)\)")
_LIST = re.compile(rb'\((?P<flags>[^)]*)\) "[^"]*" (?P<name>.+)$')
_BULK_PRECEDENCE = {"bulk", "list", "junk"}
FETCH_BATCH = 25
INBOX_BYTES = 150_000       # enough for the text of nearly any email
SENT_BYTES = 40_000         # your own emails are short; this is for style examples
TIMEOUT = 60                # seconds any one IMAP read may wait


class GmailError(Exception):
    pass


def secret_name(address: str) -> str:
    return f"gmail:{address.lower()}"


def connect(address: str, password: str | None = None) -> imaplib.IMAP4_SSL:
    password = password or get_secret(secret_name(address))
    if not password:
        raise GmailError(f"No app password stored for {address}. Run: python -m loopedin add-gmail {address}")
    # Without a timeout, a connection that dies mid-read (Wi-Fi drop) blocks forever and freezes the bot.
    conn = imaplib.IMAP4_SSL(HOST, timeout=TIMEOUT)
    try:
        conn.login(address, password.replace(" ", ""))
    except imaplib.IMAP4.error as exc:
        conn.logout()
        if "Application-specific password required" in str(exc):
            raise GmailError("That looks like your normal Google password. Gmail needs an app password "
                             "instead: create one at myaccount.google.com/apppasswords "
                             "(needs 2-Step Verification on) and paste the 16-letter code.") from exc
        raise GmailError(f"Gmail rejected the login for {address}: {exc}. "
                         "Check the app password and that IMAP is enabled.") from exc
    return conn


class Session:
    """One IMAP login reused for several reads."""

    def __init__(self, address: str):
        self.address = address
        self.conn = connect(address)
        self._folders: dict[str, str] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.conn.logout()
        except Exception:
            pass

    def special_folder(self, flag: str) -> str:
        """Find a folder by its special-use flag (\\Sent, \\All), which survives Gmail's UI language."""
        if self._folders is None:
            self._folders = {}
            status, rows = self.conn.list()
            for row in rows if status == "OK" else []:
                match = _LIST.match(row or b"")
                if not match:
                    continue
                name = match.group("name").decode("utf-8", "replace")
                for f in match.group("flags").decode().split():
                    self._folders.setdefault(f, name)
        name = self._folders.get(flag)
        if not name:
            raise GmailError(f"Could not find the Gmail folder flagged {flag}. Is it hidden from IMAP in Gmail settings?")
        return name

    def select(self, folder: str) -> None:
        status, data = self.conn.select(_quoted(folder), readonly=True)
        if status != "OK":
            raise GmailError(f"Could not open {folder}: {data}")

    def search(self, *criteria: str) -> list[bytes]:
        status, data = self.conn.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise GmailError(f"IMAP search failed: {data}")
        return data[0].split()

    def fetch(self, uids: list[bytes], max_bytes: int = INBOX_BYTES) -> list[Email]:
        """Fetch in batches, and only the first `max_bytes` of each message: the text comes
        before attachments, so this reads what we need without downloading a 30 MB PDF."""
        emails = []
        for i in range(0, len(uids), FETCH_BATCH):
            chunk = b",".join(uids[i:i + FETCH_BATCH]).decode()
            status, parts = self.conn.uid("FETCH", chunk, f"(X-GM-THRID X-GM-MSGID FLAGS BODY.PEEK[]<0.{max_bytes}>)")
            if status != "OK":
                continue
            for meta, raw in _split_fetch(parts):
                emails.append(parse_message(raw, self.address, meta))
        return emails

    def inbox_since(self, since: datetime, limit: int = 200) -> list[Email]:
        self.select("INBOX")
        # IMAP SINCE only has day granularity; trim to the exact time below.
        uids = self.search("SINCE", since.strftime("%d-%b-%Y"))[-limit:]
        emails = [e for e in self.fetch(uids) if e.date is None or e.date >= since]
        return sorted(emails, key=_date_key, reverse=True)

    def thread(self, thread_id: str, limit: int = 6) -> list[Email]:
        """The latest messages in a conversation, including the ones you sent, oldest first."""
        if not thread_id.isdigit():
            return []
        self.select(self.special_folder("\\All"))
        uids = self.search("X-GM-THRID", thread_id)[-limit:]
        return sorted(self.fetch(uids), key=_date_key)

    def sent_contacts(self, days: int = 365) -> set[str]:
        """Everyone you've emailed recently: the strongest sign a sender is a real person to you."""
        self.select(self.special_folder("\\Sent"))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        uids = self.search("SINCE", since)
        contacts: set[str] = set()
        for i in range(0, len(uids), 500):
            chunk = b",".join(uids[i:i + 500]).decode()
            status, parts = self.conn.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (TO CC)])")
            if status != "OK":
                continue
            for part in parts:
                if isinstance(part, tuple):
                    headers = email.message_from_bytes(part[1], policy=policy.default)
                    values = [str(v) for v in headers.get_all("To", []) + headers.get_all("Cc", [])]
                    contacts.update(a.lower() for _, a in getaddresses(values) if a)
        return contacts

    def sent_to(self, recipient: str, limit: int = 5) -> list[Email]:
        """Your most recent emails to one person: the best examples of how you write to them."""
        self.select(self.special_folder("\\Sent"))
        uids = self.search("TO", f'"{recipient}"')[-limit:]
        return sorted(self.fetch(uids, SENT_BYTES), key=_date_key, reverse=True)

    def sent_since(self, since: datetime, limit: int = 50) -> list[Email]:
        self.select(self.special_folder("\\Sent"))
        uids = self.search("SINCE", since.strftime("%d-%b-%Y"))[-limit:]
        return [e for e in self.fetch(uids, SENT_BYTES) if e.date is None or e.date >= since]

    def recent_sent(self, limit: int = 150) -> list[Email]:
        self.select(self.special_folder("\\Sent"))
        uids = self.search("ALL")[-limit:]
        return sorted(self.fetch(uids, SENT_BYTES), key=_date_key, reverse=True)

    # -- drafts: the only writes this module makes ---------------------------
    def save_draft(self, raw: bytes, message_id: str) -> str:
        """Put a draft in Gmail's Drafts folder. Returns its Gmail thread id (hex), for the link."""
        folder = self.special_folder("\\Drafts")
        status, data = self.conn.append(_quoted(folder), "(\\Draft)", imaplib.Time2Internaldate(time.time()), raw)
        if status != "OK":
            raise GmailError(f"Gmail wouldn't save the draft: {data}")
        self.select(folder)
        uids = self.search("HEADER", "Message-ID", message_id)
        if not uids:
            return ""
        status, parts = self.conn.uid("FETCH", uids[-1], "(X-GM-THRID)")
        _, thrid, _ = parse_meta(parts[0] if isinstance(parts[0], bytes) else parts[0][0])
        return f"{int(thrid):x}" if thrid else ""

    def delete_draft(self, message_id: str) -> None:
        """Remove a draft this bot created (found by the Message-ID it gave it)."""
        folder = self.special_folder("\\Drafts")
        status, _ = self.conn.select(_quoted(folder))        # read-write: only ever for Drafts
        if status != "OK":
            return
        for uid in self.search("HEADER", "Message-ID", message_id):
            self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        self.conn.expunge()


def _split_fetch(parts: list) -> list[tuple[bytes, bytes]]:
    """imaplib gives a batch FETCH as (meta, literal) tuples, each maybe followed by a bytes
    element carrying the rest of that message's metadata (e.g. FLAGS after the literal)."""
    items: list[list[bytes]] = []
    for part in parts:
        if isinstance(part, tuple):
            items.append([part[0], part[1]])
        elif isinstance(part, bytes) and items:
            items[-1][0] += part
    return [(meta, raw) for meta, raw in items]


def _quoted(folder: str) -> str:
    return folder if folder.startswith('"') else f'"{folder}"'


def fetch_since(address: str, since: datetime, limit: int = 200) -> list[Email]:
    with Session(address) as session:
        return session.inbox_since(since, limit)


def _date_key(e: Email) -> datetime:
    return e.date or datetime.min.replace(tzinfo=timezone.utc)


def parse_meta(meta: bytes) -> tuple[str, str, bool]:
    thrid = msgid = ""
    seen = False
    for match in _META.finditer(meta):
        if match.group(1):
            thrid = match.group(1).decode()
        elif match.group(2):
            msgid = match.group(2).decode()
        else:
            seen = b"\\Seen" in match.group(3)
    return msgid, thrid, seen


def forwarded_via(msg: EmailMessage) -> str:
    """MIT's split delivery relays through exchange-forwarding-*.mit.edu; nothing else does."""
    for received in msg.get_all("Received", []):
        text = str(received).lower()
        if "exchange-forwarding" in text and ".mit.edu" in text:
            return "mit"
    return ""


def is_bulk(msg: EmailMessage) -> bool:
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    if str(msg.get("Precedence", "")).strip().lower() in _BULK_PRECEDENCE:
        return True
    auto = str(msg.get("Auto-Submitted", "")).strip().lower()
    return bool(auto) and auto != "no"


def parse_message(raw: bytes, account: str, meta: bytes = b"") -> Email:
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)
    gm_msgid, gm_thrid, seen = parse_meta(meta)
    message_id = (msg.get("Message-ID") or "").strip()

    senders = getaddresses([str(msg.get("From", ""))])
    sender_name, sender_addr = senders[0] if senders else ("", "")

    try:
        date = msg["Date"].datetime if msg["Date"] else None
    except (AttributeError, TypeError, ValueError):
        date = None
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)

    return Email(
        account=account,
        provider="gmail",
        id=gm_msgid or message_id,
        thread_id=gm_thrid or message_id,
        message_id=message_id,
        subject=str(msg.get("Subject", "")).strip(),
        sender_name=sender_name,
        sender_addr=sender_addr.lower(),
        to=[a.lower() for _, a in getaddresses([str(v) for v in msg.get_all("To", [])]) if a],
        cc=[a.lower() for _, a in getaddresses([str(v) for v in msg.get_all("Cc", [])]) if a],
        date=date,
        body_text=body_text(msg),
        is_read=seen,
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=str(msg.get("References", "")).split(),
        is_bulk=is_bulk(msg),
        reply_to=next((a.lower() for _, a in getaddresses([str(msg.get("Reply-To", ""))]) if a), ""),
        via=forwarded_via(msg),
        has_invite=any(p.get_content_type() in ("text/calendar", "application/ics") for p in msg.walk()),
    )


def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_subtype() == "html":
        return html_to_text(content)
    return tidy(content)
