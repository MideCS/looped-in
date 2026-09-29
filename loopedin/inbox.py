"""Fetch from every connected account, keeping one account's failure from hiding the rest."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import gmail, outlook
from .config import Account, Config
from .models import Email


@dataclass
class AccountResult:
    account: Account
    emails: list[Email] = field(default_factory=list)
    error: str = ""


def fetch_all(config: Config, since: datetime, limit: int = 50) -> list[AccountResult]:
    results = []
    for account in config.accounts:
        try:
            if account.provider == "gmail":
                emails = gmail.fetch_since(account.address, since, limit=limit)
            else:
                emails = outlook.fetch_since(config.outlook_client_id, account.address, since, limit=limit)
            results.append(AccountResult(account, emails))
        except (gmail.GmailError, outlook.OutlookError) as exc:
            results.append(AccountResult(account, error=str(exc)))
        except OSError as exc:
            results.append(AccountResult(account, error=f"Could not reach {account.provider}: {exc}"))
    return results


def parse_since(value: str) -> datetime:
    """'30m', '6h', '2d' -> that long ago, in UTC."""
    match = re.fullmatch(r"(\d+)\s*([mhd])", value.strip().lower())
    if not match:
        raise ValueError("use e.g. 30m, 6h, 2d")
    amount, unit = int(match.group(1)), match.group(2)
    unit_name = {"m": "minutes", "h": "hours", "d": "days"}[unit]
    return datetime.now(timezone.utc) - timedelta(**{unit_name: amount})
