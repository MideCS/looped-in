"""Command line for setting up accounts and checking what gets fetched.

    python -m loopedin ui
    python -m loopedin add-gmail you@gmail.com
    python -m loopedin add-outlook --client-id <azure app id>
    python -m loopedin accounts
    python -m loopedin fetch --since 24h
    python -m loopedin sort --since 24h -v
"""

import argparse
import getpass
import sys
from datetime import datetime

from . import gmail, inbox, outlook
from .config import Account, Config, data_dir
from .inbox import fetch_all
from .secrets import set_secret
from .telegram import TelegramError


def parse_since(value: str) -> datetime:
    try:
        return inbox.parse_since(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_add_gmail(args) -> int:
    address = args.address.lower()
    print("Create an app password at https://myaccount.google.com/apppasswords")
    print("(needs 2-Step Verification on). Paste it below; it is not echoed.")
    password = getpass.getpass("App password: ").strip()
    gmail.connect(address, password).logout()
    set_secret(gmail.secret_name(address), password.replace(" ", ""))
    config = Config.load()
    config.add(Account("gmail", address))
    config.save()
    print(f"Connected {address}. Password stored in Windows Credential Manager.")
    return 0


def cmd_add_outlook(args) -> int:
    config = Config.load()
    if args.client_id:
        config.outlook_client_id = args.client_id
    address = outlook.sign_in(config.outlook_client_id)
    config.add(Account("outlook", address))
    config.save()
    print(f"Connected {address}.")
    return 0


def cmd_ui(args) -> int:
    from .ui import serve
    serve(args.port, open_browser=not args.no_browser)
    return 0


def cmd_accounts(args) -> int:
    config = Config.load()
    if not config.accounts:
        print("No accounts yet. Use add-gmail / add-outlook.")
    for account in config.accounts:
        print(f"{account.provider:8} {account.address}")
    print(f"\nData folder: {data_dir()}")
    return 0


def cmd_fetch(args) -> int:
    config = Config.load()
    if not config.accounts:
        print("No accounts yet. Use add-gmail / add-outlook.")
        return 1
    failed = False
    for result in fetch_all(config, args.since, limit=args.limit):
        account = result.account
        if result.error:
            print(f"\n!! {account.address}: {result.error}")
            failed = True
            continue
        print(f"\n== {account.address} ({account.provider}) - {len(result.emails)} since {args.since.astimezone():%a %H:%M}")
        for e in result.emails:
            when = e.date.astimezone().strftime("%a %H:%M") if e.date else "?"
            unread = " " if e.is_read else "*"
            sender = e.sender_name or e.sender_addr
            print(f"{unread} {when}  {sender[:28]:28}  {e.subject[:70]}")
            if args.verbose:
                print(f"      {e.snippet}")
    return 1 if failed else 0


def cmd_sort(args) -> int:
    from . import digest, sorter
    from .store import Store

    config = Config.load()
    if not config.accounts:
        print("No accounts yet. Use add-gmail / add-outlook.")
        return 1
    store = Store()
    try:
        run = sorter.run(config, store, args.since, resort=args.resort, progress=lambda m: print(f"  {m}", flush=True))
    finally:
        store.close()
    for error in run.errors:
        print(f"!! {error}")
    print(f"\nSorted {run.sorted_now} new emails.\n")
    print(digest.build(run.items, datetime.now().astimezone()))
    if args.verbose:
        print("\n---")
        for e, s in run.items:
            if s:
                print(f"[{s.category:5}{'!' if s.urgent else ' '}] {s.source:8} {(e.sender_name or e.sender_addr)[:24]:24} "
                      f"{e.subject[:50]:50} | {s.reason}")
    return 1 if run.errors else 0


def cmd_telegram(args) -> int:
    from . import telegram

    print("Create a bot with @BotFather in Telegram (/newbot) and paste its token. It is not echoed.")
    info = telegram.save_token(getpass.getpass("Bot token: "))
    code = telegram.pairing_code()
    print(f"\nOpen https://t.me/{info['username']} in Telegram, tap Start, then send:\n\n    /start {code}\n")
    tg = telegram.Telegram()
    offset = None
    deadline = datetime.now().timestamp() + 600
    while datetime.now().timestamp() < deadline:
        chat_id, offset = telegram.wait_for_pairing(tg, code, offset)
        if chat_id:
            tg.call("getUpdates", offset=offset, timeout=0)   # acknowledge, so the bot doesn't see it again
            config = Config.load()
            config.telegram_chat_id, config.telegram_bot = chat_id, info["username"]
            config.save()
            tg.send(chat_id, "✅ Connected. Digests and urgent emails will arrive here.")
            print("Paired.")
            return 0
    print("No pairing message within 10 minutes. Run this again.")
    return 1


RESTART = 3   # exit code of a bot that wants to restart on changed code


def cmd_run(args) -> int:
    from .bot import Bot

    config = Config.load()
    if config.telegram_chat_id is None:
        print("Telegram isn't paired yet. Run: python -m loopedin telegram")
        return 1
    if args.child:
        Bot(log=lambda m: print(f"{datetime.now():%H:%M:%S} {m}", flush=True)).run_forever()
        return RESTART   # run_forever only returns when a code change asks for a restart
    # The bot runs in a child process, so after a code change it can restart on the new code.
    import subprocess
    while True:
        try:
            code = subprocess.call([sys.executable, "-m", "loopedin", "run", "--child"])
        except KeyboardInterrupt:
            return 0
        if code != RESTART:
            return code
        print(f"{datetime.now():%H:%M:%S} restarting on the new code", flush=True)


def cmd_chat(args) -> int:
    import re
    from .store import Store

    for row in Store().chat(args.limit):
        when = datetime.fromisoformat(row["at"]).astimezone()
        text = re.sub(r"<[^>]+>", "", row["text"]).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        print(f"--- {when:%a %H:%M} {row['who']}\n{text.strip()}\n")
    return 0


def cmd_digest(args) -> int:
    from .bot import Bot

    bot = Bot(log=print)
    bot.check_mail()
    bot.send_digest(on_demand=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    # The Windows console defaults to cp1252, which can't print the digest's emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="loopedin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-gmail", help="connect a Gmail account with an app password")
    p.add_argument("address")
    p.set_defaults(func=cmd_add_gmail)

    p = sub.add_parser("add-outlook", help="sign in to an Outlook/Hotmail account")
    p.add_argument("--client-id", help="Azure app registration's Application (client) ID")
    p.set_defaults(func=cmd_add_outlook)

    p = sub.add_parser("ui", help="open the control panel in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("accounts", help="list connected accounts")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("fetch", help="print recent inbox emails from every account")
    p.add_argument("--since", type=parse_since, default=parse_since("24h"))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("-v", "--verbose", action="store_true", help="show a snippet of each body")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("sort", help="sort recent email with Claude and print the digest")
    p.add_argument("--since", type=parse_since, default=parse_since("24h"))
    p.add_argument("--resort", action="store_true", help="re-sort emails already sorted (keeps your corrections)")
    p.add_argument("-v", "--verbose", action="store_true", help="list every email with its category and reason")
    p.set_defaults(func=cmd_sort)

    p = sub.add_parser("telegram", help="connect your Telegram bot")
    p.set_defaults(func=cmd_telegram)

    p = sub.add_parser("run", help="run the bot: check mail, ping for urgent email, send digests")
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("chat", help="print the recent Telegram chat transcript")
    p.add_argument("-n", "--limit", type=int, default=60)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("digest", help="check mail and send a digest to Telegram now")
    p.set_defaults(func=cmd_digest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 0
    except (gmail.GmailError, outlook.OutlookError, TelegramError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
