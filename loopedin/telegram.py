"""A small Telegram Bot API client.

The bot token lives in Windows Credential Manager. The bot only ever talks to
the one chat that paired with it (Config.telegram_chat_id); updates from any
other chat are dropped before they reach the command handlers.
"""

import html
import re
import secrets as token_gen

import requests

from .secrets import get_secret, set_secret

TOKEN_SECRET = "telegram:bot"
MAX_MESSAGE = 4096


class TelegramError(Exception):
    pass


def esc(text: str) -> str:
    """Escape for parse_mode=HTML."""
    return html.escape(text, quote=False)


_ALLOWED_TAG = re.compile(r"&lt;(/?)(b|i)&gt;")


def safe_html(text: str) -> str:
    """Escape model-written text for parse_mode=HTML, letting only <b> and <i> through."""
    return _ALLOWED_TAG.sub(r"<\1\2>", esc(text))


def plain(text: str) -> str:
    """The same text with any tags dropped, for when Telegram rejects the markup."""
    return esc(re.sub(r"</?(b|i)>", "", text))


def pairing_code() -> str:
    return f"{token_gen.randbelow(900000) + 100000}"


class Telegram:
    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        self.token = token or get_secret(TOKEN_SECRET)
        if not self.token:
            raise TelegramError("No Telegram bot token saved. Connect Telegram in the control panel.")
        self.http = session or requests.Session()
        self.on_sent = None   # optional hook(kind, text, reply_markup) for the chat transcript

    def call(self, method: str, *, http_timeout: float = 30, **params) -> dict | list | bool:
        # `http_timeout` is ours; a plain `timeout` in params is Telegram's long-poll setting.
        try:
            resp = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                                  timeout=http_timeout)
        except requests.RequestException as exc:
            raise TelegramError(f"Could not reach Telegram: {exc.__class__.__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise TelegramError(f"Telegram returned HTTP {resp.status_code}")
        if not body.get("ok"):
            raise TelegramError(f"Telegram said: {body.get('description', resp.status_code)}")
        return body["result"]

    def me(self) -> dict:
        return self.call("getMe")

    def updates(self, offset: int | None, wait: int = 25) -> list[dict]:
        params = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", http_timeout=wait + 10, **params)

    def send(self, chat_id: int, text_html: str, reply_markup: dict | None = None) -> list[dict]:
        """Send HTML text, split on line boundaries if it's over Telegram's 4096-character limit.
        Buttons go on the last piece."""
        chunks = split_message(text_html)
        sent = []
        for i, chunk in enumerate(chunks):
            extra = {"reply_markup": reply_markup} if reply_markup and i == len(chunks) - 1 else {}
            sent.append(self.call("sendMessage", chat_id=chat_id, text=chunk, parse_mode="HTML",
                                  disable_web_page_preview=True, **extra))
            if self.on_sent:
                self.on_sent("bot", chunk, extra.get("reply_markup"))
        return sent

    def edit(self, chat_id: int, message_id: int, text_html: str, reply_markup: dict | None = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text_html, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            self.call("editMessageText", **params)
        except TelegramError as exc:
            if "message is not modified" not in str(exc):
                raise
            return
        if self.on_sent:
            self.on_sent("bot (edited)", text_html, reply_markup)

    def answer(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def save_token(token: str) -> dict:
    """Check a token with Telegram, then store it. Returns the bot's getMe info."""
    info = Telegram(token.strip()).me()
    set_secret(TOKEN_SECRET, token.strip())
    return info


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line[:limit]
        else:
            current = candidate[:limit]
    if current:
        chunks.append(current)
    return chunks


def wait_for_pairing(tg: Telegram, code: str, offset: int | None = None, wait: int = 20) -> tuple[int | None, int | None]:
    """One long-poll for '/start <code>'. Returns (chat_id or None, next offset)."""
    for update in tg.updates(offset, wait=wait):
        offset = update["update_id"] + 1
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if text in (f"/start {code}", code) and message.get("chat", {}).get("type") == "private":
            return message["chat"]["id"], offset
    return None, offset
