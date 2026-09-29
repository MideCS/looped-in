r"""Where state lives and what accounts exist.

Everything goes under %LOCALAPPDATA%\looped-in rather than the repo: the repo
sits in OneDrive, and neither mail data nor login tokens should be synced.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP = "looped-in"
DEFAULT_DIGEST_TIMES = ("08:00", "13:00", "18:00")


def data_dir() -> Path:
    base = os.environ.get("LOOPEDIN_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Account:
    provider: str   # "gmail" | "outlook"
    address: str


@dataclass
class Config:
    accounts: list[Account] = field(default_factory=list)
    outlook_client_id: str = ""
    telegram_chat_id: int | None = None     # the one chat the bot will talk to
    telegram_bot: str = ""                  # the bot's @username, for links
    digest_times: list[str] = field(default_factory=lambda: list(DEFAULT_DIGEST_TIMES))
    aliases: dict[str, str] = field(default_factory=dict)   # e.g. {"mit": "you@mit.edu"}: forwarded-in mailboxes

    def my_addresses(self) -> set[str]:
        return {a.address.lower() for a in self.accounts} | {v.lower() for v in self.aliases.values()}

    @classmethod
    def load(cls) -> "Config":
        path = data_dir() / "config.json"
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            accounts=[Account(**a) for a in raw.get("accounts", [])],
            outlook_client_id=raw.get("outlook_client_id", ""),
            telegram_chat_id=raw.get("telegram_chat_id"),
            telegram_bot=raw.get("telegram_bot", ""),
            digest_times=raw.get("digest_times") or list(DEFAULT_DIGEST_TIMES),
            aliases=raw.get("aliases") or {},
        )

    def save(self) -> None:
        path = data_dir() / "config.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def add(self, account: Account) -> None:
        self.remove(account.provider, account.address)
        self.accounts.append(account)

    def remove(self, provider: str, address: str) -> None:
        self.accounts = [a for a in self.accounts
                         if (a.provider, a.address.lower()) != (provider, address.lower())]
