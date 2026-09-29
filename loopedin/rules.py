"""The decisions that don't need a model, and the signals that help the model with the rest."""

import re

from .models import Email
from .store import Sort

_CODE_WORDS = re.compile(
    r"\b(verification|security|confirmation|login|log-in|sign[- ]?in|one[- ]time|2fa|two[- ]factor|passcode|otp)\b"
    r"[^.\n]{0,40}\b(code|pin|password)\b|\byour code is\b",
    re.IGNORECASE)
_DIGITS = re.compile(r"\b\d{4,8}\b")


def is_code_email(e: Email) -> bool:
    if _CODE_WORDS.search(e.subject):
        return True
    head = e.body_text[:800]
    return bool(_CODE_WORDS.search(head) and _DIGITS.search(head))


def pre_sort(e: Email, sender_rules: dict[str, str]) -> Sort | None:
    """A final answer for the cases rules can settle, or None to ask the model."""
    if sender_rules.get(e.sender_addr) == "mute":
        return Sort("noise", False, e.subject, "", "You muted this sender.", "rule")
    if is_code_email(e):
        # You asked for these yourself and are already watching for them; never worth a ping.
        return Sort("fyi", False, "Sign-in or verification code", "", "Looks like a one-time code.", "rule")
    return None


def signals(e: Email, *, me: set[str], contacts: set[str], sender_rules: dict[str, str],
            thread: list[Email]) -> list[str]:
    out = []
    if sender_rules.get(e.sender_addr) == "vip":
        out.append("You marked this sender as VIP.")
    if e.sender_addr in contacts:
        out.append("You have emailed this sender in the past year.")
    else:
        out.append("You have not emailed this sender in the past year.")
    if any(m.sender_addr in me for m in thread):
        out.append("You have written in this conversation before.")
    if any(a in me for a in e.to):
        out.append("Sent directly to you (you are in To).")
    elif any(a in me for a in e.cc):
        out.append("You are only cc'd.")
    else:
        out.append("You are not in To or Cc (mailing list, alias or bcc).")
    if e.is_bulk:
        out.append("Has mailing-list / automated-sender headers.")
    return out


def apply_vip(s: Sort, e: Email, sender_rules: dict[str, str]) -> Sort:
    """VIPs always interrupt and never sink below 'read', whatever the model thought."""
    if sender_rules.get(e.sender_addr) != "vip":
        return s
    s.urgent = True
    if s.category in ("fyi", "noise"):
        s.category = "read"
    return s
