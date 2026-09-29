"""The long-running bot: check mail, ping for urgent email, send digests on schedule, answer commands.

One thread long-polls Telegram and hands updates to the main loop through a
queue, so every database write happens on the main thread.
"""

import json
import queue
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone

from . import assistant, digest, gmail, meetings, selfedit, sorter, style, tune
from .claude import ClaudeError
from .config import Config
from .replies import Replies
from .store import Store, key_of
from .telegram import Telegram, TelegramError, esc, plain, safe_html

MAIL_EVERY = timedelta(minutes=2)
FIRST_WINDOW = timedelta(hours=24)       # what the very first digest covers
ON_DEMAND_WINDOW = timedelta(hours=24)   # what /digest shows: everything open from the last day
LOOKBACK = timedelta(days=3)             # how far back an undigested email can still make a digest
OVERLAP = timedelta(minutes=15)          # re-check a little before the last check, in case of clock skew

HELP = "\n\n".join([
    "<b>looped-in</b>",
    "<b>Reply to an email:</b> tap ✍️ under it, or type its number and what to say, "
    "e.g. <i>2: thanks, Thursday works</i>. You'll get a draft to open in Gmail.",
    "<b>Done with one:</b> tap ✓ Done, or type <i>2 done</i>.",
    "/digest — send a digest now",
    "/status — is everything working?",
    "/style — how I write your replies",
    "<i>style: never sign off with Best,</i> — add a rule for drafts",
    "/cancel — stop the reply in progress",
    "/ping — check I'm awake",
    "/tune — change how I behave, e.g. <i>/tune keep answers to two lines</i> (or just tell me)",
    "<b>Anything else</b> — just ask, e.g. <i>what did Sam want?</i>, "
    "<i>tell me about the E14 hack</i>, <i>dismiss the Google one</i>.",
])


def spaced(text: str) -> str:
    """One blank line between every line of a model answer, so bullets don't run together."""
    return "\n\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def due_slot(now: datetime, times: list[str], last: datetime | None) -> datetime | None:
    """The latest scheduled digest time that has passed with no digest sent since."""
    local = now.astimezone()
    slots = []
    for day in (local.date() - timedelta(days=1), local.date()):
        for t in times:
            hour, minute = (int(x) for x in t.split(":"))
            slots.append(datetime.combine(day, dtime(hour, minute), tzinfo=local.tzinfo))
    passed = [s for s in slots if s <= local]
    if not passed:
        return None
    latest = max(passed)
    return latest if last is None or last < latest else None


def next_slot(now: datetime, times: list[str]) -> datetime:
    local = now.astimezone()
    for day in (local.date(), local.date() + timedelta(days=1)):
        for t in sorted(times):
            hour, minute = (int(x) for x in t.split(":"))
            slot = datetime.combine(day, dtime(hour, minute), tzinfo=local.tzinfo)
            if slot > local:
                return slot
    raise ValueError("no digest times configured")


class Bot:
    def __init__(self, store: Store | None = None, tg: Telegram | None = None, log=print):
        self.store = store or Store()
        self.tg = tg or Telegram()
        self.log = log
        self.inbox: queue.Queue = queue.Queue()
        self.replies = Replies(self)
        self.tg.on_sent = self._record_sent
        self.coding = threading.Lock()   # one code change at a time
        self.restart = False             # set after a code change; run_forever returns so the new code loads

    def _record_sent(self, who: str, text: str, markup: dict | None) -> None:
        buttons = [b["text"] for row in (markup or {}).get("inline_keyboard", []) for b in row]
        self.store.add_chat(who, text + (f"\n[buttons: {' | '.join(buttons)}]" if buttons else ""))

    @property
    def config(self) -> Config:
        return Config.load()

    def say(self, text_html: str) -> None:
        chat = self.config.telegram_chat_id
        if chat is None:
            raise TelegramError("Telegram isn't paired yet.")
        self.tg.send(chat, text_html)

    # -- mail ----------------------------------------------------------------
    def check_mail(self) -> None:
        config = self.config
        last = self.store.get_meta("last_check")
        since = datetime.fromisoformat(last) - OVERLAP if last else now_utc() - FIRST_WINDOW
        run = sorter.run(config, self.store, since, progress=lambda m: None)
        self.store.set_meta("last_check", now_utc().isoformat())
        self._report_errors(run.errors)
        self._safely(self.check_meetings, run)

        floor_text = self.store.get_meta("alerts_since")
        floor = datetime.fromisoformat(floor_text) if floor_text else now_utc()
        labels = digest.account_labels([e for e, _ in run.items])
        single = len(config.accounts) + len(config.aliases) <= 1
        for e, s in run.items:
            key = key_of(e)
            if s and s.urgent and e.date and e.date >= floor and not self.store.was_alerted(key):
                self.tg.send(config.telegram_chat_id, digest.urgent_html(e, s, "" if single else labels[digest.source(e)]),
                             reply_markup=digest.urgent_keyboard(key))
                self.store.mark_alerted(key)
                self.log(f"urgent ping: {e.subject[:60]}")

    def check_meetings(self, run) -> None:
        """Offer any meeting you've agreed to in new mail, received or sent, for your calendar."""
        config = self.config
        started = self.store.get_meta("meetings_since")
        if started is None:   # only mail from now on; don't dig up old plans
            self.store.set_meta("meetings_since", now_utc().isoformat())
            return
        since = datetime.fromisoformat(started)
        seen = set(json.loads(self.store.get_meta("meetings_seen") or "[]"))
        received = [e for e, s in run.items if not (s and s.category == "noise")]
        sent = []
        for account in config.accounts:
            if account.provider == "gmail":
                try:
                    with gmail.Session(account.address) as session:
                        sent += session.sent_since(since - OVERLAP)
                except (gmail.GmailError, OSError) as exc:
                    self.log(f"couldn't read sent mail for meetings: {exc}")
        fresh = [e for e in received + sent if key_of(e) not in seen and (e.date is None or e.date >= since - OVERLAP)]
        if not fresh:
            return
        events = meetings.find(fresh, me=config.my_addresses(), now=now_utc())
        for ev in events:
            event_id = self.store.add_event(key_of(ev.email), ev.title, ev.start, ev.who or ev.email.thread_id)
            if event_id is None:
                continue
            self.tg.send(config.telegram_chat_id, meetings.card_html(ev, config.my_addresses()),
                         reply_markup=meetings.card_keyboard(event_id, ev))
            self.log(f"meeting offered: {ev.title} at {ev.start:%a %d %b %H:%M}")
        seen |= {key_of(e) for e in fresh}
        self.store.set_meta("meetings_seen", json.dumps(sorted(seen)[-800:]))

    def _report_errors(self, errors: list[str]) -> None:
        """Tell you about a problem once, not every two minutes."""
        text = "\n".join(errors)
        if text and text != self.store.get_meta("last_error"):
            self.say("⚠️ <b>Problem checking mail</b>\n" + esc(text))
        self.store.set_meta("last_error", text)
        for error in errors:
            self.log(f"error: {error}")

    # -- digests -------------------------------------------------------------
    def send_digest(self, on_demand: bool = False) -> bool:
        """Scheduled: what's new since the last digest, and only if something needs you or is worth
        reading. On demand (/digest): everything you haven't dismissed from the last day, always."""
        config = self.config
        accounts = {a.address for a in config.accounts}
        if on_demand:
            recent = self.store.open_items(now_utc() - ON_DEMAND_WINDOW, accounts)
            older_new = self.store.undigested(now_utc() - LOOKBACK, accounts)
            seen = {key_of(e) for e, _ in recent}
            items = recent + [(e, s) for e, s in older_new if key_of(e) not in seen]
        else:
            since = now_utc() - (LOOKBACK if self.store.last_digest_at() else FIRST_WINDOW)
            items = self.store.undigested(since, accounts)
        d = digest.compose(items, now_utc())
        if on_demand and d.empty:
            self.say("✓ All clear. Nothing from the last 24 hours is waiting on you.")
            return False
        if not on_demand and not (d.needs or d.reads):
            return False  # stay quiet; FYI and noise roll into the next real digest
        numbered = {key_of(x.email): x.number for x in d.entries}
        counted = [key_of(e) for e, s in items if key_of(e) not in numbered]
        digest_id = self.store.record_digest(numbered, counted)   # first, so the buttons can name it
        try:
            sent = self.tg.send(config.telegram_chat_id, digest.render(d), reply_markup=digest.keyboard(digest_id, d))
        except Exception:
            self.store.delete_digest(digest_id)
            raise
        self.store.set_digest_message(digest_id, sent[-1]["message_id"])
        self.log(f"digest sent: {len(d.needs)} need you, {len(d.reads)} to read")
        return True

    def maybe_send_scheduled(self) -> None:
        last = self.store.get_meta("last_digest_slot")
        slot = due_slot(now_utc(), self.config.digest_times, datetime.fromisoformat(last) if last else None)
        if slot:
            self.store.set_meta("last_digest_slot", slot.isoformat())
            self.send_digest()

    # -- telegram ------------------------------------------------------------
    def status_html(self) -> str:
        config = self.config
        last = self.store.get_meta("last_check")
        last_txt = datetime.fromisoformat(last).astimezone().strftime("%I:%M %p").lstrip("0") if last else "never"
        nxt = next_slot(now_utc(), config.digest_times).strftime("%a %I:%M %p").replace(" 0", " ")
        accounts = ", ".join(esc(a.address) for a in config.accounts) or "none"
        return "\n\n".join(["✅ <b>Running</b>", f"<b>Accounts:</b> {accounts}",
                             f"<b>Last mail check:</b> {last_txt}", f"<b>Next digest:</b> {nxt}"])

    def handle_button(self, query: dict) -> None:
        """Digest expand/collapse, ✍️ reply buttons, and draft Change/Skip."""
        message = query.get("message") or {}
        if (message.get("chat") or {}).get("id") != self.config.telegram_chat_id:
            return  # not you
        data = query.get("data") or ""
        labels = {b.get("callback_data"): b["text"] for row in (message.get("reply_markup") or {})
                  .get("inline_keyboard", []) for b in row}
        self.store.add_chat("you (tapped)", f"{labels.get(data, '?')}  [{data}]")
        if data.startswith("dn:"):
            key = data[3:]
            self.replies.dismiss(key)
            email = self.store.email(key)
            holding = self.store.digest_holding(key)
            if email and not (holding and holding[1] == message["message_id"]):
                self.tg.edit(message["chat"]["id"], message["message_id"], digest.done_html(email))
            self.tg.answer(query["id"], "Done")
            return
        if data.startswith("ev:"):
            parts = data.split(":")
            row = self.store.event(int(parts[1])) if len(parts) == 3 and parts[1].isdigit() else None
            if row:
                self.store.set_event_status(row["id"], "skipped")
                self.tg.edit(message["chat"]["id"], message["message_id"], f"✕ <s>{esc(row['title'])}</s>")
            self.tg.answer(query["id"], "Skipped")
            return
        if data == "code:go":
            request = self.store.get_meta("code_request") or ""
            if not request or self.coding.locked():
                self.tg.answer(query["id"], "Already working on one" if request else "Nothing to change")
                return
            self.store.set_meta("code_request", "")
            self.tg.answer(query["id"], "On it")
            self.tg.edit(message["chat"]["id"], message["message_id"],
                         f"🛠 Changing the code: <i>{esc(request)}</i>\n\nThis takes a few minutes. "
                         "I'll message you when it's done.")
            threading.Thread(target=self._safely, args=(self.change_code, request), daemon=True).start()
            return
        if data.startswith("code:undo:"):
            self.tg.answer(query["id"])
            try:
                result = selfedit.undo(data.split(":", 2)[2])
            except ClaudeError as exc:
                result = f"Couldn't undo: {exc}"
            self.tg.edit(message["chat"]["id"], message["message_id"], f"↩ {esc(result)}")
            if result == "Undone.":
                self.say("🔄 Restarting with the old code…")
                self.restart = True
            return
        if data == "tune:undo":
            undone = tune.undo(self.store)
            self.tg.edit(message["chat"]["id"], message["message_id"],
                         "↩ <i>Undone: back to your previous settings.</i>" if undone
                         else "<i>Nothing to undo.</i>")
            self.tg.answer(query["id"], "Undone" if undone else "")
            return
        if data.startswith("rk:"):
            self.tg.answer(query["id"])
            self.replies.choose(data[3:])
            return
        reply = digest.parse_reply_callback(data)
        if reply:
            self.tg.answer(query["id"])
            key = self.store.digest_numbers(reply[0]).get(reply[1])
            if key:
                self.replies.choose(key)
            return
        toast = self.replies.on_button(data, message["chat"]["id"], message["message_id"])
        if toast is not None:
            self.tg.answer(query["id"], toast)
            return
        parsed = digest.parse_callback(data)
        contents = self.store.digest_contents(parsed[0]) if parsed else None
        if not contents:
            self.tg.answer(query["id"], "That digest is gone.")
            return
        digest_id, show_read, show_fyi = parsed
        # Remember which sections are open, so striking an item through later keeps them open.
        self.store.set_meta(f"digest_view:{digest_id}", f"{int(show_read)}{int(show_fyi)}")
        d = self._rebuild(digest_id, contents)
        self.tg.edit(message["chat"]["id"], message["message_id"],
                     digest.render(d, show_read=show_read, show_fyi=show_fyi),
                     reply_markup=digest.keyboard(digest_id, d, show_read, show_fyi))
        self.tg.answer(query["id"])

    def _rebuild(self, digest_id: int, contents) -> "digest.Digest":
        entries = contents[0]
        done = self.store.dismissed([f"{e.provider}:{e.id}" for _, e, _ in entries])
        return digest.from_numbered(*contents, done=done)

    def refresh_digest_for(self, key: str) -> None:
        """Re-render the digest an email was in, e.g. to strike it through once it's done."""
        holding = self.store.digest_holding(key)
        if holding:
            self.refresh_digest(holding[0])

    def refresh_digest(self, digest_id: int) -> None:
        message_id = self.store.digest_message(digest_id)
        if message_id is None:
            return
        contents = self.store.digest_contents(digest_id)
        if not contents:
            return
        d = self._rebuild(digest_id, contents)
        view = self.store.get_meta(f"digest_view:{digest_id}") or "00"
        show_read, show_fyi = view[0] == "1", view[1] == "1"
        try:
            self.tg.edit(self.config.telegram_chat_id, message_id,
                         digest.render(d, show_read=show_read, show_fyi=show_fyi),
                         reply_markup=digest.keyboard(digest_id, d, show_read, show_fyi))
        except TelegramError as exc:   # e.g. the message is too old to edit
            self.log(f"couldn't update digest {digest_id}: {exc}")

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self.handle_button(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat is None or chat != self.config.telegram_chat_id:
            return  # not you: ignore without replying
        text = (message.get("text") or "").strip()
        self.store.add_chat("you", text or "(not text)")
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command == "/digest":
            self.check_mail()
            self.send_digest(on_demand=True)
        elif command == "/status":
            self.say(self.status_html())
        elif command == "/style":
            self.say("<b>How I write your replies</b>\n\n" + esc(style.describe(self.store)))
        elif command == "/tune":
            request = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if request:
                self.tune(request)
            else:
                self.say("<b>Your settings</b>\n\n" + esc(tune.describe(self.store)))
        elif command == "/cancel":
            self.replies.cancel()
            self.say("OK, cancelled.")
        elif command == "/ping":
            self.say("pong")
        elif command.startswith("/") or not text:
            self.say(HELP)
        elif not self.replies.on_text(text):
            self.ask_assistant(text)

    def tune(self, request: str) -> None:
        chat = self.config.telegram_chat_id
        try:
            self.tg.call("sendChatAction", chat_id=chat, action="typing")
            change = tune.apply(self.store, request)
        except (ClaudeError, OSError) as exc:
            self.say(f"⚠️ Couldn't change that: {esc(str(exc))}")
            return
        lines = [f"🔧 {esc(change.summary)}" if change.summary else "🔧 Done."] if change.changed else []
        if change.changed:
            lines.append(f"<i>Changed: {esc(', '.join(change.changed))}</i>")
        buttons = [{"text": "↩ Undo", "callback_data": "tune:undo"}] if change.changed else []
        if change.needs_code:
            self.store.set_meta("code_request", request)
            lines.append(f"🛠 {esc(change.needs_code)}\n\nI can change the code for this on the laptop: "
                         "Claude edits it, the tests have to pass, and then I restart with it.")
            buttons.append({"text": "🛠 Change the code", "callback_data": "code:go"})
        if not lines:
            lines = [f"🔧 {esc(change.summary or 'Nothing to change.')}"]
        self.tg.send(chat, "\n\n".join(lines), reply_markup={"inline_keyboard": [buttons]} if buttons else None)
        self.log(f"tuned: {', '.join(change.changed) or 'nothing'}")

    def change_code(self, request: str) -> None:
        with self.coding:
            self.log(f"changing code: {request[:80]}")
            try:
                outcome = selfedit.change(request)
            except (ClaudeError, OSError) as exc:
                self.say(f"⚠️ The code change failed, nothing was changed: {esc(str(exc))}")
                return
            if not outcome.ok:
                self.say(f"🛠 {esc(outcome.message)}")
                return
            self.tg.send(self.config.telegram_chat_id,
                         f"🛠 {esc(outcome.message)}\n\n<i>Tests pass. Committed on the laptop as "
                         f"{outcome.commit} (not pushed). Files: {esc(outcome.files)}</i>\n\n🔄 Restarting with it…",
                         reply_markup={"inline_keyboard": [[{"text": "↩ Undo",
                                                             "callback_data": f"code:undo:{outcome.commit}"}]]})
            self.log(f"code changed: {outcome.commit}")
            self.restart = True

    def ask_assistant(self, text: str) -> None:
        chat = self.config.telegram_chat_id
        try:
            self.tg.call("sendChatAction", chat_id=chat, action="typing")
            result = assistant.ask(self.store, text, {a.address for a in self.config.accounts})
        except ClaudeError as exc:
            self.say(f"⚠️ I couldn't think that through: {esc(str(exc))}")
            return
        if result.answer:
            answer = spaced(result.answer)
            try:
                self.say(safe_html(answer))
            except TelegramError:      # unbalanced tags from the model: fall back to plain text
                self.say(plain(answer))
        if result.action == "show_digest":
            self.check_mail()
            self.send_digest(on_demand=True)
        elif result.action == "reply" and result.notes:
            self.replies.draft(result.key, result.notes)
        elif result.action in ("reply", "start_reply"):
            self.replies.choose(result.key)
        elif result.action == "tune":
            self.tune(result.notes or text)
        elif result.action == "dismiss":
            self.replies.dismiss_many(result.keys)
            emails = [e for e in (self.store.email(k) for k in result.keys) if e]
            if emails:
                self.say("\n".join(digest.done_line(e) for e in emails) + "\n<i>Dismissed</i>")

    def _poll_telegram(self) -> None:
        offset = self.store.get_meta("telegram_offset")
        offset = int(offset) if offset else None
        while True:
            try:
                for update in self.tg.updates(offset):
                    offset = update["update_id"] + 1
                    self.inbox.put(update)
            except TelegramError as exc:
                self.log(f"telegram: {exc}")
                time.sleep(10)

    # -- main loop -----------------------------------------------------------
    def run_forever(self) -> None:
        if self.store.get_meta("alerts_since") is None:
            # Urgent pings only for mail that arrives from now on; older urgent mail goes in the digest.
            self.store.set_meta("alerts_since", now_utc().isoformat())
        last = self.store.get_meta("last_check")
        if last and now_utc() - datetime.fromisoformat(last) > timedelta(minutes=30):
            self.say("👋 Back online, catching up on mail.")
        threading.Thread(target=self._poll_telegram, daemon=True).start()
        self.log("running; Ctrl+C to stop")

        next_mail = now_utc()
        while not self.restart:
            wait = max(0.0, min(30.0, (next_mail - now_utc()).total_seconds()))
            try:
                update = self.inbox.get(timeout=wait)
                self.store.set_meta("telegram_offset", str(update["update_id"] + 1))
                self._safely(self.handle, update)
            except queue.Empty:
                pass
            if now_utc() >= next_mail:
                self._safely(self.check_mail)
                next_mail = now_utc() + MAIL_EVERY
            self._safely(self.maybe_send_scheduled)

    def _safely(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # the bot must outlive any single failure
            self.log(f"error in {fn.__name__}: {exc!r}")
