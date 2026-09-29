"""Outlook / Hotmail through Microsoft Graph.

Microsoft switched off app passwords for personal accounts, so this signs in
once with a device code ("go to microsoft.com/devicelogin, enter ABC123") and
keeps a refresh token in a DPAPI-encrypted cache that only your Windows user
can decrypt.
"""

from datetime import datetime, timezone

import msal
import requests
from msal_extensions import PersistedTokenCache, build_encrypted_persistence

from .config import data_dir
from .models import Email
from .text import tidy

AUTHORITY = "https://login.microsoftonline.com/common"
# Asked for up front so later steps (mark read, send) don't force a second sign-in.
SCOPES = ["Mail.ReadWrite", "Mail.Send", "User.Read"]
GRAPH = "https://graph.microsoft.com/v1.0"
_SELECT = ",".join([
    "id", "conversationId", "internetMessageId", "subject", "from", "toRecipients",
    "ccRecipients", "receivedDateTime", "body", "isRead",
])


class OutlookError(Exception):
    pass


def _app(client_id: str) -> msal.PublicClientApplication:
    if not client_id:
        raise OutlookError("No Outlook client id configured. Run: python -m loopedin add-outlook --client-id <id>")
    cache = PersistedTokenCache(build_encrypted_persistence(str(data_dir() / "outlook_token_cache.bin")))
    return msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)


def sign_in(client_id: str) -> str:
    """Interactive device-code sign-in. Returns the signed-in address."""
    app, flow = start_sign_in(client_id)
    print(flow["message"], flush=True)
    return finish_sign_in(app, flow)


def start_sign_in(client_id: str) -> tuple[msal.PublicClientApplication, dict]:
    """Get a device code. The flow's `verification_uri` and `user_code` are what the user needs."""
    app = _app(client_id)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OutlookError(f"Could not start sign-in: {flow.get('error_description', flow)}")
    return app, flow


def finish_sign_in(app: msal.PublicClientApplication, flow: dict) -> str:
    """Block until the user enters the code (or it expires). Returns the signed-in address."""
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise OutlookError(f"Sign-in failed: {result.get('error_description', result)}")
    me = requests.get(f"{GRAPH}/me", headers=_auth(result["access_token"]), timeout=30)
    me.raise_for_status()
    body = me.json()
    return (body.get("mail") or body.get("userPrincipalName") or "").lower()


def _token(client_id: str, address: str) -> str:
    app = _app(client_id)
    accounts = app.get_accounts(username=address) or app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result or "access_token" not in result:
        raise OutlookError(f"Outlook sign-in for {address} has expired. Run: python -m loopedin add-outlook")
    return result["access_token"]


def sign_out(client_id: str, address: str) -> None:
    if not client_id:
        return
    app = _app(client_id)
    for account in app.get_accounts(username=address):
        app.remove_account(account)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_since(client_id: str, address: str, since: datetime, limit: int = 200) -> list[Email]:
    headers = _auth(_token(client_id, address)) | {"Prefer": 'outlook.body-content-type="text"'}
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{GRAPH}/me/mailFolders/inbox/messages"
    params = {
        "$filter": f"receivedDateTime ge {stamp}",
        "$orderby": "receivedDateTime desc",
        "$select": _SELECT,
        "$top": "50",
    }
    emails: list[Email] = []
    while url and len(emails) < limit:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise OutlookError(f"Graph returned {resp.status_code}: {resp.text[:300]}")
        page = resp.json()
        emails.extend(parse_message(m, address) for m in page.get("value", []))
        url, params = page.get("@odata.nextLink"), None
    return emails[:limit]


def _addr(recipient: dict | None) -> tuple[str, str]:
    info = (recipient or {}).get("emailAddress") or {}
    return info.get("name", ""), (info.get("address") or "").lower()


def parse_message(m: dict, account: str) -> Email:
    name, addr = _addr(m.get("from"))
    received = m.get("receivedDateTime")
    return Email(
        account=account,
        provider="outlook",
        id=m["id"],
        thread_id=m.get("conversationId") or m["id"],
        message_id=m.get("internetMessageId") or "",
        subject=(m.get("subject") or "").strip(),
        sender_name=name,
        sender_addr=addr,
        to=[a for _, a in map(_addr, m.get("toRecipients") or []) if a],
        cc=[a for _, a in map(_addr, m.get("ccRecipients") or []) if a],
        date=datetime.fromisoformat(received) if received else None,
        body_text=tidy((m.get("body") or {}).get("content") or ""),
        is_read=bool(m.get("isRead")),
    )
