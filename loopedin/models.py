from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Email:
    account: str            # the address this arrived at, e.g. you@gmail.com
    provider: str           # "gmail" | "outlook"
    id: str                 # provider-unique id, stable across fetches
    thread_id: str          # groups a conversation (Gmail X-GM-THRID / Outlook conversationId)
    message_id: str         # RFC 5322 Message-ID, needed to thread replies
    subject: str
    sender_name: str
    sender_addr: str
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    date: datetime | None = None
    body_text: str = ""
    is_read: bool = False
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    is_bulk: bool = False   # mailing-list / automated headers (List-Unsubscribe, Precedence: bulk, ...)
    reply_to: str = ""      # where replies should go, if not the sender
    via: str = ""           # "mit" when MIT forwarded it here from your mit.edu mailbox
    has_invite: bool = False  # carries a calendar invitation (text/calendar), so it's on your calendar already

    @property
    def snippet(self) -> str:
        return " ".join(self.body_text.split())[:160]
