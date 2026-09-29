"""Turn sorted emails into the digest: plain text for the panel, one HTML message for Telegram.

The Telegram message shows what needs you, with context. "To read" and "FYI"
start collapsed behind buttons and expand in place when tapped. Items are
numbered across all sections so a reply like "4: sounds good" names exactly one
email; the numbering is saved with the digest (see Store.record_digest).
"""

from dataclasses import dataclass, field
from datetime import datetime

from .models import Email
from .store import Sort
from .telegram import MAX_MESSAGE, esc


@dataclass
class Entry:
    number: int
    email: Email
    sort: Sort
    label: str          # which account, e.g. "Gmail"; "" when you only have one
    done: bool = False  # you dismissed it after the digest went out


@dataclass
class Digest:
    header: str
    needs: list[Entry] = field(default_factory=list)
    reads: list[Entry] = field(default_factory=list)
    fyis: list[Entry] = field(default_factory=list)
    noise: int = 0

    @property
    def entries(self) -> list[Entry]:
        return self.needs + self.reads + self.fyis

    @property
    def empty(self) -> bool:
        return not (self.entries or self.noise)


def source(e: Email) -> str:
    """Which inbox an email really belongs to: a forwarded-in mailbox like MIT, or the account."""
    return e.via or e.account


def account_labels(emails: list[Email]) -> dict[str, str]:
    """Label per source: 'MIT' for forwarded MIT mail, 'Gmail' / 'Outlook' for accounts, or the
    part before @ when you have two accounts of the same kind."""
    accounts = {(e.account, e.provider) for e in emails if not e.via}
    per_provider: dict[str, int] = {}
    for _, provider in accounts:
        per_provider[provider] = per_provider.get(provider, 0) + 1
    labels = {address: (address.split("@")[0] if per_provider[provider] > 1 else provider.capitalize())
              for address, provider in accounts}
    labels.update({e.via: e.via.upper() for e in emails if e.via})
    return labels


def header(when: datetime, needs: int) -> str:
    local = when.astimezone()
    icon = "☀️" if local.hour < 12 else "🌤" if local.hour < 17 else "🌙"
    clock = local.strftime("%I:%M %p").lstrip("0")
    if not needs:
        return f"{icon} {clock} digest — nothing needs you"
    return f"{icon} {clock} digest — {needs} need{'s' if needs == 1 else ''} you"


def _rank(pair: tuple[Email, Sort]):
    e, s = pair
    return (not s.urgent, -(e.date.timestamp() if e.date else 0))


def compose(items: list[tuple[Email, Sort | None]], now: datetime) -> Digest:
    sorted_items = [(e, s) for e, s in items if s is not None]
    labels = account_labels([e for e, _ in sorted_items])
    single = len(labels) <= 1
    by = {c: sorted((p for p in sorted_items if p[1].category == c), key=_rank) for c in ("reply", "read", "fyi")}
    d = Digest(header(now, len(by["reply"])), noise=sum(1 for _, s in sorted_items if s.category == "noise"))
    number = 0
    for target, group in ((d.needs, by["reply"]), (d.reads, by["read"]), (d.fyis, by["fyi"])):
        for e, s in group:
            number += 1
            target.append(Entry(number, e, s, "" if single else labels[source(e)]))
    return d


def from_numbered(entries: list[tuple[int, Email, Sort]], noise: int, sent_at: datetime,
                  done: set[str] = frozenset()) -> Digest:
    """Rebuild a sent digest from its saved numbering, so its buttons can expand it later."""
    labels = account_labels([e for _, e, _ in entries])
    single = len(labels) <= 1
    d = Digest("", noise=noise)
    for number, e, s in sorted(entries, key=lambda t: t[0]):
        target = {"reply": d.needs, "read": d.reads}.get(s.category, d.fyis)
        target.append(Entry(number, e, s, "" if single else labels[source(e)],
                            done=f"{e.provider}:{e.id}" in done))
    d.header = header(sent_at, sum(not x.done for x in d.needs))
    return d


# -- rendering ----------------------------------------------------------------
def _who(x: Entry, html: bool) -> str:
    name = x.email.sender_name or x.email.sender_addr
    name = f"<b>{esc(name)}</b>" if html else name
    return f"{name} · {esc(x.label) if html else x.label}" if x.label else name


def _need_block(x: Entry, html: bool) -> list[str]:
    if x.done:
        name = x.email.sender_name or x.email.sender_addr
        return [f"✓ <s>{x.number}. {esc(name)}</s>" if html else f"✓ {x.number}. {name} (done)"]
    flag = "🔴 " if x.sort.urgent else ""
    summary = x.sort.summary or x.email.subject
    lines = [f"{flag}{x.number}. {_who(x, html)}", esc(summary) if html else summary]
    if x.sort.blurb:
        lines.append(f"<i>↳ {esc(x.sort.blurb)}</i>" if html else f"↳ {x.sort.blurb}")
    return lines


def _short_line(x: Entry, html: bool) -> str:
    summary = x.sort.summary or x.email.subject
    line = f"{x.number}. {_who(x, html)} — {esc(summary) if html else summary}"
    if x.done:
        return f"✓ <s>{line}</s>" if html else f"✓ {line}"
    return line


def open_count(entries: list[Entry]) -> int:
    return sum(not x.done for x in entries)


def render(d: Digest, *, html: bool = True, show_read: bool = False, show_fyi: bool = False) -> str:
    b = (lambda t: f"<b>{esc(t)}</b>") if html else (lambda t: t)
    blocks = [b(d.header)]
    for x in d.needs:
        blocks.append("\n".join(_need_block(x, html)))
    if not d.needs:
        blocks.append("Nothing needs a reply. ✓")
    if show_read and d.reads:
        blocks.append("\n\n".join([b(f"📖 To read ({open_count(d.reads)})")] + [_short_line(x, html) for x in d.reads]))
    if show_fyi and (d.fyis or d.noise):
        lines = [b(f"📦 FYI ({open_count(d.fyis)})")] + [_short_line(x, html) for x in d.fyis]
        if d.noise:
            lines.append(f"🗑 {d.noise} noise hidden")
        blocks.append("\n\n".join(lines))
    text = "\n\n".join(blocks)
    if not html:
        return text
    if len(text) + len(SPACER) > MAX_MESSAGE:
        # Telegram can't edit a message into several; trim the FYI list to fit.
        keep = max(0, len(d.fyis) - (len(text) - MAX_MESSAGE) // 60 - 5)
        trimmed = Digest(d.header, d.needs, d.reads, d.fyis[:keep], d.noise)
        text = render(trimmed, html=True, show_read=show_read, show_fyi=show_fyi).removesuffix(SPACER)
        text += f"\n…and {len(d.fyis) - keep} more FYI"
    return text + SPACER


# Telegram trims trailing blank lines, but not a line holding a braille blank (U+2800), so this
# keeps a gap between the last email and the buttons under the message.
SPACER = "\n⠀"


REPLY_BUTTONS_PER_ROW = 4


def keyboard(digest_id: int, d: Digest, show_read: bool = False, show_fyi: bool = False) -> dict | None:
    """A ✍️ button per email that needs you, then buttons that expand/collapse To read and FYI in place."""
    def data(r: bool, f: bool) -> str:
        return f"dg:{digest_id}:{int(r)}{int(f)}"

    replies = [{"text": f"✍️ {x.number}", "callback_data": f"rn:{digest_id}:{x.number}"}
               for x in d.needs if not x.done]
    rows = [replies[i:i + REPLY_BUTTONS_PER_ROW] for i in range(0, len(replies), REPLY_BUTTONS_PER_ROW)]
    row = []
    if d.reads:
        label = "▾ Hide to read" if show_read else f"📖 To read ({open_count(d.reads)})"
        row.append({"text": label, "callback_data": data(not show_read, show_fyi)})
    if d.fyis or d.noise:
        label = "▾ Hide FYI" if show_fyi else f"📦 FYI ({open_count(d.fyis)})"
        row.append({"text": label, "callback_data": data(show_read, not show_fyi)})
    if row:
        rows.append(row)
    return {"inline_keyboard": rows} if rows else None


def parse_reply_callback(data: str) -> tuple[int, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "rn" or not (parts[1].isdigit() and parts[2].isdigit()):
        return None
    return int(parts[1]), int(parts[2])


def parse_callback(data: str) -> tuple[int, bool, bool] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "dg" or not parts[1].isdigit() or len(parts[2]) != 2:
        return None
    return int(parts[1]), parts[2][0] == "1", parts[2][1] == "1"


def render_text(d: Digest) -> str:
    """Everything expanded, for the control panel's preview."""
    return render(d, html=False, show_read=True, show_fyi=True)


def build(items: list[tuple[Email, Sort | None]], now: datetime) -> str:
    return render_text(compose(items, now))


def urgent_html(e: Email, s: Sort, label: str) -> str:
    who = esc(e.sender_name or e.sender_addr) + (f" · {esc(label)}" if label else "")
    lines = [f"🔴 <b>Urgent</b> — <b>{who}</b>", esc(s.summary or e.subject)]
    if s.blurb:
        lines.append(f"<i>↳ {esc(s.blurb)}</i>")
    return "\n\n".join([lines[0], "\n".join(lines[1:])]) + SPACER


def urgent_keyboard(key: str) -> dict | None:
    # Telegram caps callback data at 64 bytes; long Outlook ids simply don't get buttons.
    if len(f"rk:{key}".encode()) > 64:
        return None
    return {"inline_keyboard": [[{"text": "✍️ Reply", "callback_data": f"rk:{key}"},
                                 {"text": "✓ Done", "callback_data": f"dn:{key}"}]]}


def done_line(e: Email) -> str:
    return f"✓ <s>{esc(e.sender_name or e.sender_addr)} — {esc(e.subject)}</s>"


def done_html(e: Email) -> str:
    return f"✓ <s>{esc(e.sender_name or e.sender_addr)} — {esc(e.subject)}</s>\n<i>Dismissed</i>"
