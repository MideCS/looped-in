"""Local control panel: connect accounts and preview the combined inbox.

Listens on 127.0.0.1 only. Two guards keep other websites open in your browser
from driving it: every API call must carry a per-run token that is only ever
written into this server's own page, and requests whose Host isn't localhost
are refused (which stops DNS-rebinding tricks).
"""

import json
import re
import secrets as token_gen
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import digest, gmail, inbox, outlook, sorter
from .config import Account, Config, data_dir
from .secrets import delete_secret, set_secret
from .store import Store, key_of
from . import telegram

PAGE = Path(__file__).with_name("ui.html")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OutlookSignIn:
    """The one device-code sign-in that can be in flight at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"status": "idle"}

    def start(self, client_id: str) -> dict:
        app, flow = outlook.start_sign_in(client_id)
        with self.lock:
            self.state = {
                "status": "pending",
                "code": flow["user_code"],
                "url": flow["verification_uri"],
                "expires_at": flow.get("expires_at", time.time() + 900),
            }
        threading.Thread(target=self._finish, args=(client_id, app, flow), daemon=True).start()
        return self.snapshot()

    def _finish(self, client_id, app, flow):
        try:
            address = outlook.finish_sign_in(app, flow)
            config = Config.load()
            config.outlook_client_id = client_id
            config.add(Account("outlook", address))
            config.save()
            result = {"status": "done", "address": address}
        except Exception as exc:  # surfaced to the page, not swallowed
            result = {"status": "error", "error": str(exc)}
        with self.lock:
            self.state = result

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


class SortJob:
    """The one sort run that can be in flight at a time; the page polls its status."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"status": "idle"}

    def start(self, since, resort: bool) -> dict:
        with self.lock:
            if self.state.get("status") == "running":
                return dict(self.state)
            self.state = {"status": "running", "message": "Starting…"}
        threading.Thread(target=self._run, args=(since, resort), daemon=True).start()
        return self.snapshot()

    def _progress(self, message: str) -> None:
        with self.lock:
            self.state["message"] = message

    def _run(self, since, resort):
        store = Store()
        try:
            run = sorter.run(Config.load(), store, since, resort=resort, progress=self._progress)
            result = {"status": "done", "sorted_now": run.sorted_now, "errors": run.errors, "limit_hit": run.limit_hit}
        except Exception as exc:  # surfaced to the page, not swallowed
            result = {"status": "error", "errors": [str(exc)]}
        finally:
            store.close()
        with self.lock:
            self.state = result

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


class TelegramPairing:
    """Waits for '/start <code>' from your Telegram after you paste a bot token."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"status": "idle"}

    def start(self, token: str) -> dict:
        info = telegram.save_token(token)
        code = telegram.pairing_code()
        with self.lock:
            self.state = {"status": "pending", "bot": info["username"], "code": code}
        threading.Thread(target=self._wait, args=(info["username"], code), daemon=True).start()
        return self.snapshot()

    def _wait(self, bot: str, code: str):
        tg = telegram.Telegram()
        offset, deadline = None, time.time() + 600
        try:
            while time.time() < deadline:
                with self.lock:
                    if self.state.get("code") != code:
                        return  # a newer attempt replaced this one
                chat_id, offset = telegram.wait_for_pairing(tg, code, offset, wait=10)
                if chat_id:
                    tg.call("getUpdates", offset=offset, timeout=0)  # acknowledge the /start
                    config = Config.load()
                    config.telegram_chat_id, config.telegram_bot = chat_id, bot
                    config.save()
                    tg.send(chat_id, "✅ Connected. Digests and urgent emails will arrive here.")
                    result = {"status": "done", "bot": bot}
                    break
            else:
                result = {"status": "error", "error": "No pairing message within 10 minutes. Try again."}
        except Exception as exc:  # surfaced to the page, not swallowed
            result = {"status": "error", "error": str(exc)}
        with self.lock:
            self.state = result

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


def sort_json(s) -> dict | None:
    return asdict(s) if s else None


def email_json(e, s=None) -> dict:
    data = asdict(e)
    data["date"] = e.date.isoformat() if e.date else None
    data["snippet"] = e.snippet
    data["key"] = key_of(e)
    data["sort"] = sort_json(s)
    return data


def make_handler(token: str, port: int, sign_in: OutlookSignIn, sort_job: SortJob, pairing: TelegramPairing):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "looped-in"

        def log_message(self, fmt, *args):
            pass

        # -- plumbing ---------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _guard(self, api: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "bad host"})
                return False
            if api and not token_gen.compare_digest(self.headers.get("X-Loopedin-Token", ""), token):
                self._json(403, {"error": "bad token"})
                return False
            return True

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(min(length, 65536)))

        # -- routes -----------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                if not self._guard(api=False):
                    return
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._guard(api=True):
                return

            if url.path == "/api/accounts":
                config = Config.load()
                return self._json(200, {
                    "accounts": [asdict(a) for a in config.accounts],
                    "outlook_client_id": config.outlook_client_id,
                    "data_dir": str(data_dir()),
                    "telegram": {"bot": config.telegram_bot, "paired": config.telegram_chat_id is not None},
                    "digest_times": config.digest_times,
                })
            if url.path == "/api/emails":
                query = parse_qs(url.query)
                try:
                    since = inbox.parse_since(query.get("since", ["24h"])[0])
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                results = inbox.fetch_all(Config.load(), since, limit=100)
                store = Store()
                try:
                    store.save_emails([e for r in results for e in r.emails])
                    sorts = store.sorts([key_of(e) for r in results for e in r.emails])
                finally:
                    store.close()
                return self._json(200, {"results": [
                    {"account": asdict(r.account), "error": r.error,
                     "emails": [email_json(e, sorts.get(key_of(e))) for e in r.emails]}
                    for r in results
                ]})
            if url.path == "/api/telegram/status":
                return self._json(200, pairing.snapshot())
            if url.path == "/api/sort/status":
                return self._json(200, sort_job.snapshot())
            if url.path == "/api/digest":
                query = parse_qs(url.query)
                try:
                    since = inbox.parse_since(query.get("since", ["24h"])[0])
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                store = Store()
                try:
                    accounts = {a.address for a in Config.load().accounts}
                    emails = store.emails_since(since, accounts)
                    sorts = store.sorts([key_of(e) for e in emails])
                finally:
                    store.close()
                items = [(e, sorts.get(key_of(e))) for e in emails]
                return self._json(200, {"text": digest.build(items, datetime.now(timezone.utc)),
                                        "unsorted": sum(1 for _, s in items if s is None)})
            if url.path == "/api/outlook/status":
                return self._json(200, sign_in.snapshot())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(api=True):
                return
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "bad json"})
            path = urlparse(self.path).path

            if path == "/api/gmail":
                address = str(body.get("address", "")).strip().lower()
                password = str(body.get("password", "")).replace(" ", "")
                if not EMAIL_RE.match(address) or not password:
                    return self._json(400, {"error": "Enter the Gmail address and the 16-letter app password."})
                try:
                    gmail.connect(address, password).logout()
                except gmail.GmailError as exc:
                    return self._json(400, {"error": str(exc)})
                except OSError as exc:
                    return self._json(502, {"error": f"Could not reach Gmail: {exc}"})
                set_secret(gmail.secret_name(address), password)
                config = Config.load()
                config.add(Account("gmail", address))
                config.save()
                return self._json(200, {"ok": True, "address": address})

            if path == "/api/outlook/start":
                client_id = str(body.get("client_id", "")).strip() or Config.load().outlook_client_id
                if not client_id:
                    return self._json(400, {"error": "Paste the Application (client) ID from the Azure portal."})
                try:
                    return self._json(200, sign_in.start(client_id))
                except (outlook.OutlookError, ValueError) as exc:
                    return self._json(400, {"error": str(exc)})
                except OSError as exc:
                    return self._json(502, {"error": f"Could not reach Microsoft: {exc}"})

            if path == "/api/telegram/token":
                token = str(body.get("token", "")).strip()
                if ":" not in token:
                    return self._json(400, {"error": "Paste the whole token BotFather gave you (it contains a colon)."})
                try:
                    return self._json(200, pairing.start(token))
                except telegram.TelegramError as exc:
                    return self._json(400, {"error": str(exc)})

            if path == "/api/telegram/test":
                config = Config.load()
                if config.telegram_chat_id is None:
                    return self._json(400, {"error": "Telegram isn't paired yet."})
                try:
                    telegram.Telegram().send(config.telegram_chat_id, "👋 Test from your looped-in control panel.")
                except telegram.TelegramError as exc:
                    return self._json(502, {"error": str(exc)})
                return self._json(200, {"ok": True})

            if path == "/api/telegram/disconnect":
                config = Config.load()
                config.telegram_chat_id, config.telegram_bot = None, ""
                config.save()
                delete_secret(telegram.TOKEN_SECRET)
                return self._json(200, {"ok": True})

            if path == "/api/sort":
                try:
                    since = inbox.parse_since(str(body.get("since", "24h")))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                if not Config.load().accounts:
                    return self._json(400, {"error": "Connect an account first."})
                return self._json(200, sort_job.start(since, bool(body.get("resort"))))

            if path == "/api/correct":
                store = Store()
                try:
                    corrected = store.correct(str(body.get("key", "")), str(body.get("category", "")),
                                              bool(body.get("urgent")))
                except KeyError:
                    return self._json(404, {"error": "That email hasn't been sorted yet."})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                finally:
                    store.close()
                return self._json(200, {"sort": sort_json(corrected)})

            if path == "/api/accounts/remove":
                provider, address = body.get("provider"), str(body.get("address", ""))
                config = Config.load()
                if provider == "gmail":
                    delete_secret(gmail.secret_name(address))
                elif provider == "outlook":
                    outlook.sign_out(config.outlook_client_id, address)
                config.remove(provider, address)
                config.save()
                return self._json(200, {"ok": True})

            return self._json(404, {"error": "not found"})

    return Handler


def make_server(port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    token = token_gen.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), None)
    actual_port = server.server_address[1]
    server.RequestHandlerClass = make_handler(token, actual_port, OutlookSignIn(), SortJob(), TelegramPairing())
    return server, token


def serve(port: int = 8765, open_browser: bool = True) -> None:
    server, _ = make_server(port)
    url = f"http://localhost:{server.server_address[1]}/"
    print(f"looped-in control panel: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
