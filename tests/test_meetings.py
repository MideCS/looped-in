from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from loopedin import bot as botmod, gmail, meetings
from loopedin.config import Account, Config
from loopedin.models import Email
from loopedin.sorter import SortRun
from loopedin.store import Sort, Store

ME = "me@gmail.com"
CHAT = 42
NOW = datetime.now(timezone.utc)


def mail(i, body="Sure, let's do Tuesday 11-12:30!", sender=ME, **kw) -> Email:
    return Email(account=ME, provider="gmail", id=str(i), thread_id=f"t{i}", message_id=f"<{i}@x>",
                 subject="Acme Intern Program Follow Up", sender_name=kw.pop("name", "Me"), sender_addr=sender,
                 to=kw.pop("to", ["jordan@example.com"]), date=NOW - timedelta(minutes=1), body_text=body, **kw)


def local(days, hour, minute=0):
    d = (NOW.astimezone() + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return d.strftime("%Y-%m-%dT%H:%M")


def test_only_mail_mentioning_a_time_and_not_an_invite_reaches_claude():
    keep = mail(1)
    assert meetings.candidates([keep, mail(2, body="Thanks so much!"), mail(3, has_invite=True)]) == [keep]


def test_find_keeps_future_events_and_fills_in_a_default_end(monkeypatch):
    monkeypatch.setattr(meetings, "run_structured", lambda prompt, **kw: {"events": [
        {"id": "m1", "title": "Call with Jordan", "start": local(2, 11), "end": "", "location": "Zoom",
         "with": "Jordan Lee", "evidence": "let's do Tuesday 11-12:30"},
        {"id": "m1", "title": "Already happened", "start": local(-2, 11), "end": "", "location": "",
         "with": "x", "evidence": ""},
        {"id": "m9", "title": "Not a real email", "start": local(2, 11), "end": "", "location": "", "with": "x",
         "evidence": ""},
    ]})
    [ev] = meetings.find([mail(1)], me={ME}, now=NOW)
    assert ev.title == "Call with Jordan" and ev.end - ev.start == meetings.DEFAULT_LENGTH


def test_calendar_link_is_prefilled_in_utc():
    start = datetime(2026, 9, 29, 11, 0, tzinfo=timezone(timedelta(hours=-4)))
    ev = meetings.Event(mail(1), "Call with Jordan", start, start + timedelta(minutes=90), "Zoom", "Jordan", "11-12:30")
    q = parse_qs(urlparse(meetings.calendar_link(ev)).query)
    assert q["action"] == ["TEMPLATE"] and q["text"] == ["Call with Jordan"] and q["location"] == ["Zoom"]
    assert q["dates"] == ["20260929T150000Z/20260929T163000Z"]


# -- bot ----------------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.sent, self.edited = [], None

    def send(self, chat_id, text_html, reply_markup=None):
        self.sent.append((text_html, reply_markup))
        return [{"message_id": 1}]

    def edit(self, chat_id, message_id, text_html, reply_markup=None):
        self.edited = text_html

    def answer(self, callback_id, text=""):
        pass


class FakeSession:
    def __init__(self, address):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sent_since(self, since):
        return [mail(1)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = Config(accounts=[Account("gmail", ME)], telegram_chat_id=CHAT)
    monkeypatch.setattr(botmod.Config, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(gmail, "Session", FakeSession)
    store = Store(tmp_path / "t.db")
    tg = FakeTelegram()
    yield botmod.Bot(store=store, tg=tg, log=lambda m: None), store, tg
    store.close()


def test_agreed_meeting_is_offered_once_with_a_calendar_button(env, monkeypatch):
    b, store, tg = env
    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return {"events": [{"id": "m1", "title": "Call with Jordan", "start": local(2, 11), "end": local(2, 12, 30),
                            "location": "", "with": "Jordan Lee", "evidence": "let's do Tuesday 11-12:30"}]}
    monkeypatch.setattr(meetings, "run_structured", fake)
    store.set_meta("meetings_since", (NOW - timedelta(hours=1)).isoformat())

    b.check_meetings(SortRun(items=[]))
    b.check_meetings(SortRun(items=[]))          # same sent email again: not re-read, not re-offered
    assert len(calls) == 1 and len(tg.sent) == 1
    text, markup = tg.sent[0]
    assert "Call with Jordan" in text and "your email to jordan@example.com" in text
    add, skip = markup["inline_keyboard"][0]
    assert add["url"].startswith(meetings.CALENDAR) and skip["callback_data"] == "ev:1:skip"

    b.handle_button({"id": "q", "data": "ev:1:skip", "message": {"chat": {"id": CHAT}, "message_id": 7}})
    assert store.event(1)["status"] == "skipped" and "<s>Call with Jordan</s>" in tg.edited


def test_first_run_only_starts_the_clock(env, monkeypatch):
    b, store, tg = env
    monkeypatch.setattr(meetings, "run_structured", lambda *a, **k: pytest.fail("shouldn't scan old mail"))
    b.check_meetings(SortRun(items=[(mail(5), Sort("reply", False))]))
    assert store.get_meta("meetings_since") and not tg.sent


def test_invites_are_detected_when_parsing():
    raw = (b"From: a@b.co\r\nTo: me@gmail.com\r\nSubject: Invitation\r\nDate: Mon, 28 Sep 2026 20:00:00 +0000\r\n"
           b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
           b"--x\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n--x--\r\n")
    assert gmail.parse_message(raw, ME).has_invite
