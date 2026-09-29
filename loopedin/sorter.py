"""Fetch -> cache -> rules -> Claude -> save. Each email is sorted once; corrections are never overwritten."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import gmail, rules
from .claude import ClaudeError, LimitReached
from .classify import Item, classify, fallback
from .config import Config, data_dir
from .inbox import fetch_all
from .models import Email
from .store import Sort, Store, key_of


def load_priorities() -> str:
    """priorities.md in the data folder: what matters to you, learned from your sent mail. You can edit it."""
    path = data_dir() / "priorities.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""

CONTACTS_MAX_AGE = timedelta(hours=24)


@dataclass
class SortRun:
    items: list[tuple[Email, Sort | None]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    sorted_now: int = 0
    limit_hit: bool = False


def _looks_like_reply(e: Email) -> bool:
    return bool(e.in_reply_to or e.references) or e.subject.lower().startswith(("re:", "aw:", "sv:"))


def refresh_contacts(store: Store, config: Config, force: bool = False) -> None:
    stamp = store.get_meta("contacts_refreshed_at")
    fresh = stamp and datetime.now(timezone.utc) - datetime.fromisoformat(stamp) < CONTACTS_MAX_AGE
    if fresh and not force:
        return
    contacts: set[str] = set()
    for account in config.accounts:
        if account.provider == "gmail":
            with gmail.Session(account.address) as session:
                contacts |= session.sent_contacts()
    store.replace_contacts(contacts)


def _needs_sort(s: Sort | None, resort: bool) -> bool:
    if s is None or s.source == "fallback":
        return True
    return resort and s.source != "you"


def run(config: Config, store: Store, since: datetime, *, resort: bool = False,
        progress: Callable[[str], None] = lambda _: None) -> SortRun:
    result = SortRun()
    me = config.my_addresses()

    progress("Fetching email…")
    emails: list[Email] = []
    for r in fetch_all(config, since, limit=200):
        if r.error:
            result.errors.append(f"{r.account.address}: {r.error}")
        emails.extend(r.emails)
    store.save_emails(emails)

    try:
        progress("Checking who you email…")
        refresh_contacts(store, config)
    except (gmail.GmailError, OSError) as exc:
        result.errors.append(f"Couldn't read Sent mail, so 'people you email' is out of date: {exc}")
    contacts = store.contacts()
    sender_rules = store.sender_rules()
    existing = store.sorts([key_of(e) for e in emails])

    pending: list[Email] = []
    for e in emails:
        key = key_of(e)
        if not _needs_sort(existing.get(key), resort):
            continue
        pre = rules.pre_sort(e, sender_rules)
        if pre:
            store.save_sort(key, pre)
            result.sorted_now += 1
        else:
            pending.append(e)

    items = _gather_context(pending, me, contacts, sender_rules, progress, result)
    if items:
        progress(f"Sorting {len(items)} emails with Claude…")
        try:
            verdicts = classify(items, me=me, corrections=store.corrections(), now=datetime.now(timezone.utc),
                                priorities=load_priorities())
        except LimitReached:
            result.limit_hit = True
            verdicts = {}
            result.errors.append("Your Claude usage limit is reached; sorted by headers only until it resets.")
        except ClaudeError as exc:
            verdicts = {}
            result.errors.append(f"Claude failed, sorted by headers only: {exc}")
        for item in items:
            s = verdicts.get(item.key) or fallback(item.email)
            store.save_sort(item.key, rules.apply_vip(s, item.email, sender_rules))
            result.sorted_now += 1

    final = store.sorts([key_of(e) for e in emails])
    result.items = [(e, final.get(key_of(e))) for e in emails]
    result.items.sort(key=lambda pair: pair[0].date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    progress("Done.")
    return result


def _gather_context(pending, me, contacts, sender_rules, progress, result) -> list[Item]:
    """Pull earlier messages for replies, one IMAP login per account."""
    by_account: dict[str, list[Email]] = {}
    for e in pending:
        by_account.setdefault(e.account, []).append(e)
    items: list[Item] = []
    for account, group in by_account.items():
        threads: dict[str, list[Email]] = {}
        wanted = {e.thread_id for e in group if e.provider == "gmail" and _looks_like_reply(e)}
        if wanted:
            progress(f"Reading {len(wanted)} earlier conversations…")
            try:
                with gmail.Session(account) as session:
                    for thread_id in wanted:
                        threads[thread_id] = session.thread(thread_id)
            except (gmail.GmailError, OSError) as exc:
                result.errors.append(f"{account}: couldn't read earlier messages for context: {exc}")
        for e in group:
            earlier = [m for m in threads.get(e.thread_id, [])
                       if m.id != e.id and (not e.date or not m.date or m.date <= e.date)][-4:]
            items.append(Item(key_of(e), e, rules.signals(
                e, me=me, contacts=contacts, sender_rules=sender_rules, thread=earlier), earlier))
    return items
