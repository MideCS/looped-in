"""Replying from Telegram: pick an email, say roughly what to reply, get a draft in your voice
saved inside the Gmail conversation, and open it in the Gmail app to send.

Flow state (which email you're replying to, or which draft you're changing) is
kept in meta as a small JSON blob and expires after REPLY_TTL.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import drafting, gmail, style
from .claude import ClaudeError
from .models import Email
from .telegram import esc

REPLY_TTL = timedelta(minutes=30)
NUMBERED = re.compile(r"^\s*(\d{1,3})\s*[:.)\-–]\s*(.+)$", re.DOTALL)
# "2 done", "1, 3 done", "1 and 2 dismiss", "done 4": the word exactly once, before or after the numbers.
NUMBER_DONE = re.compile(r"^\s*(?:(done|dismiss|ignore)\s+)?(#?\d{1,3}(?:\s*(?:,|&|and|\s)\s*#?\d{1,3})*)"
                         r"\s*[:.)\-–]?\s*(done|dismiss|ignore)?\s*$", re.IGNORECASE)
# "dismiss this", "done", "no reply needed", "ignore it" -- said instead of what to reply
DISMISS = re.compile(r"^\s*(done|dismiss|ignore|no reply( needed)?|nothing|skip)\b", re.IGNORECASE)
# Addresses that don't read replies: offer Done, never a draft.
# Answers to "What should change?" that mean "nothing, it's fine".
KEEP = re.compile(r"^\s*(lgtm|looks good( to me)?|all good|it'?s (good|fine)|(that'?s )?(good|fine|perfect|great)|"
                  r"no changes?|nothing|never ?mind|nvm|ok(ay)?|keep( it)?)\s*[.!👍]*\s*$", re.IGNORECASE)
NO_REPLY = re.compile(r"(no-?reply|do-?not-?reply|donotreply|mailer-daemon|notifications?@)", re.IGNORECASE)
SPACER = "\n⠀"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Replies:
    def __init__(self, bot):
        self.bot = bot

    @property
    def store(self):
        return self.bot.store

    @property
    def tg(self):
        return self.bot.tg

    @property
    def chat(self) -> int:
        return self.bot.config.telegram_chat_id

    # -- pending state -------------------------------------------------------
    def _set_pending(self, kind: str, value) -> None:
        self.store.set_meta("pending", json.dumps({"kind": kind, "value": value, "at": _now().isoformat()}))

    def _pending(self) -> dict | None:
        raw = self.store.get_meta("pending")
        if not raw:
            return None
        pending = json.loads(raw)
        if _now() - datetime.fromisoformat(pending["at"]) > REPLY_TTL:
            self.store.delete_meta("pending")
            return None
        return pending

    def cancel(self) -> None:
        self.store.delete_meta("pending")

    # -- entry points ----------------------------------------------------------
    def choose(self, key: str) -> None:
        """You tapped ✍️ on an email: ask what to say (or offer Done if it can't take a reply)."""
        email = self.store.email(key)
        if email is None:
            self.bot.say("I can't find that email any more.")
            return
        who = esc(email.sender_name or email.sender_addr)
        done = done_keyboard(key, "✓ Done, no reply needed")
        if NO_REPLY.search(email.reply_to or email.sender_addr):
            self.tg.send(self.chat, f"<b>{who}</b> sends from an address that doesn't read replies.\n"
                                    f"<i>{esc(email.subject)}</i>", reply_markup=done)
            return
        if email.provider != "gmail":
            self.tg.send(self.chat, "Replying to Outlook/MIT email from here isn't ready yet.", reply_markup=done)
            return
        self._set_pending("reply", key)
        self.tg.send(self.chat, f"✍️ Replying to <b>{who}</b>\n<i>{esc(email.subject)}</i>\n\n"
                                "What do you want to say? Rough notes are fine. /cancel to stop.", reply_markup=done)

    def dismiss_many(self, keys: list[str]) -> None:
        for key in keys:
            self.dismiss(key, refresh=False)
        holding = {self.store.digest_holding(k) for k in keys} - {None}
        for digest_id, _ in holding:
            self.bot.refresh_digest(digest_id)

    def dismiss(self, key: str, refresh: bool = True) -> None:
        """Done with an email: out of future digests, struck through in the one it was in,
        and any draft for it removed from Gmail."""
        self.cancel()
        self.store.dismiss(key)
        for row in self.store.open_drafts(key):
            self._discard_gmail_draft(row)
            self.store.set_draft_status(row["id"], "skipped")
        if refresh:
            self.bot.refresh_digest_for(key)

    def on_text(self, text: str) -> bool:
        """Handle a plain message if it's part of a reply. Returns False if it isn't."""
        if text.lower().startswith("style:"):
            rule = text.split(":", 1)[1].strip()
            if rule:
                self.store.add_note("rule", rule)
                self.bot.say(f"Got it. Every draft will follow this from now on:\n<i>{esc(rule)}</i>")
            return True
        match = NUMBER_DONE.match(text)
        if match and bool(match.group(1)) != bool(match.group(3)):
            numbers = list(dict.fromkeys(int(n) for n in re.findall(r"\d+", match.group(2))))
            known = self.store.digest_numbers()
            missing = [n for n in numbers if n not in known]
            if missing:
                self.bot.say(f"There's no {', '.join(f'#{n}' for n in missing)} in the latest digest.")
                return True
            self.dismiss_many([known[n] for n in numbers])
            self.bot.say(f"✓ {', '.join(f'#{n}' for n in numbers)} done.")
            return True
        match = NUMBERED.match(text)
        if match:
            key = self.store.digest_numbers().get(int(match.group(1)))
            if not key:
                self.bot.say(f"There's no #{match.group(1)} in the latest digest.")
                return True
            self.draft(key, match.group(2).strip())
            return True
        pending = self._pending()
        if pending and pending["kind"] == "reply" and DISMISS.match(text):
            self.dismiss(pending["value"])
            self.bot.say("✓ Done. No reply needed.")
            return True
        if pending and pending["kind"] == "reply":
            self.draft(pending["value"], text)
            return True
        if pending and pending["kind"] == "change":
            if KEEP.match(text):
                self.cancel()
                self.bot.say("👍 Keeping the draft as it is.")
                return True
            self.store.add_note("feedback", text)
            self.redraft(int(pending["value"]), text)
            return True
        return False

    def on_button(self, data: str, chat_id: int, message_id: int) -> str | None:
        """Handle draft buttons. Returns a short toast for the tap, or None if it wasn't ours."""
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "dr" or not parts[1].isdigit():
            return None
        draft_id, action = int(parts[1]), parts[2]
        row = self.store.draft(draft_id)
        if row is None or row["status"] != "open":
            return "That draft is closed."
        if action == "change":
            self._set_pending("change", draft_id)
            self.bot.say("✏️ What should change? e.g. <i>shorter</i>, <i>more formal</i>, <i>say I'm free Thursday</i>")
            return ""
        if action == "skip":
            self._discard_gmail_draft(row)
            self.store.set_draft_status(draft_id, "skipped")
            self.tg.edit(chat_id, message_id, "❌ Draft skipped." if not row["gmail_message_id"]
                         else "❌ Draft skipped and removed from Gmail.")
            return "Skipped"
        return None

    # -- drafting --------------------------------------------------------------
    def draft(self, key: str, notes: str) -> None:
        self.cancel()
        email = self.store.email(key)
        if email is None:
            self.bot.say("I can't find that email any more.")
            return
        if email.provider != "gmail":
            self.bot.say("Replying to Outlook/MIT email from here isn't ready yet.")
            return
        self._write(email, notes)

    def redraft(self, draft_id: int, change: str) -> None:
        self.cancel()
        row = self.store.draft(draft_id)
        email = self.store.email(row["key"]) if row else None
        if row is None or email is None:
            self.bot.say("That draft is gone.")
            return
        self._discard_gmail_draft(row)
        self.store.set_draft_status(draft_id, "replaced")
        self._write(email, row["instruction"], previous=row["body"], change=change)

    def _write(self, email: Email, notes: str, previous: str | None = None, change: str | None = None) -> None:
        who = esc(email.sender_name or email.sender_addr)
        placeholder = self.tg.send(self.chat, f"✍️ Writing your reply to <b>{who}</b>…")[-1]["message_id"]
        try:
            if not style.guide(self.store):
                self.tg.edit(self.chat, placeholder, "📚 Learning how you write from your Sent mail "
                                                     "(one time only, about a minute)…")
                style.build_guide(self.store, email.account)
                self.tg.edit(self.chat, placeholder, f"✍️ Writing your reply to <b>{who}</b>…")
            me = self.bot.config.my_addresses()
            my_name = self.store.get_meta("my_name") or ""
            with gmail.Session(email.account) as session:
                thread = [m for m in session.thread(email.thread_id)
                          if m.id != email.id and (not email.date or not m.date or m.date <= email.date)][-4:]
                examples = session.sent_to(email.reply_to or email.sender_addr)
                result = drafting.write(email=email, thread=thread, examples=examples,
                                        guide=style.guide(self.store), rules=style.rules(self.store),
                                        feedback=style.feedback(self.store), notes=notes, me=me,
                                        my_name=my_name, previous=previous, change=change)
                via_alias = self.bot.config.aliases.get(email.via, "") if email.via else ""
                if via_alias:
                    # Forwarded-in mail (MIT) is answered from Outlook, so it goes out from that address.
                    message_id, link = "", drafting.outlook_link(email, result.body)
                else:
                    raw, message_id = drafting.as_mime(email, result.body, my_name)
                    link = drafting.gmail_link(email.account, session.save_draft(raw, message_id))
        except (ClaudeError, gmail.GmailError, OSError) as exc:
            self.tg.edit(self.chat, placeholder, f"⚠️ Couldn't write that draft: {esc(str(exc))}")
            self.bot.log(f"draft failed: {exc!r}")
            return

        draft_id = self.store.add_draft(f"{email.provider}:{email.id}", notes, result.body, message_id, link)
        self.tg.edit(self.chat, placeholder, render_draft(email, result, via_alias),
                     reply_markup=draft_keyboard(draft_id, link, copy=result.body if via_alias else None))
        self.bot.log(f"draft {draft_id} ready for {email.subject[:50]}")

    def _discard_gmail_draft(self, row) -> None:
        email = self.store.email(row["key"])
        if not email or not row["gmail_message_id"]:
            return
        try:
            with gmail.Session(email.account) as session:
                session.delete_draft(row["gmail_message_id"])
        except (gmail.GmailError, OSError) as exc:
            self.bot.log(f"couldn't remove old Gmail draft: {exc!r}")


def render_draft(email: Email, result: drafting.Draft, outlook_from: str = "") -> str:
    """The draft card. Outlook drafts put the text in a <pre> block, which Telegram gives a Copy button,
    in case the Outlook link had to leave the body out."""
    who = esc(email.sender_name or email.sender_addr)
    sent_as = f"\nFrom <b>{esc(outlook_from)}</b>" if outlook_from else ""
    body = f"<pre>{esc(result.body[:3500])}</pre>" if outlook_from else esc(result.body[:3500])
    parts = [f"✉️ <b>Draft to {who}</b>{sent_as}\n<i>{esc(drafting.reply_subject(email.subject))}</i>", body]
    if result.missing:
        parts.append(f"⚠️ <i>You didn't say: {esc(result.missing)}</i>")
    if not outlook_from:
        parts.append("Saved in Gmail Drafts. Open it, check it, and press Send there.")
    else:
        copy = "Tap 📋 Copy" if len(result.body) <= MAX_COPY else "Tap Copy on the reply above"
        parts.append(f"{copy}, then ↗ Outlook: it opens searched for this email. Tap it, tap Reply and paste.")
    return "\n\n".join(parts) + SPACER


def done_keyboard(key: str, label: str = "✓ Done") -> dict | None:
    if len(f"dn:{key}".encode()) > 64:
        return None
    return {"inline_keyboard": [[{"text": label, "callback_data": f"dn:{key}"}]]}


MAX_COPY = 256   # Telegram's limit for a copy button; longer replies use the Copy label on the <pre> block


def draft_keyboard(draft_id: int, link: str, copy: str | None = None) -> dict:
    """Gmail drafts get one button. Outlook replies (copy = the reply) get Telegram's own copy button,
    which copies with no page in between, next to the link that opens Outlook."""
    if copy is None:
        first = [{"text": "📧 Open in Gmail", "url": link}]
    else:
        first = [{"text": "↗ Outlook", "url": link}]
        if len(copy) <= MAX_COPY:
            first.insert(0, {"text": "📋 Copy", "copy_text": {"text": copy}})
    return {"inline_keyboard": [
        first,
        [{"text": "✏️ Change", "callback_data": f"dr:{draft_id}:change"},
         {"text": "❌ Skip", "callback_data": f"dr:{draft_id}:skip"}],
    ]}
