from datetime import datetime, timezone

from loopedin import gmail, outlook
from loopedin.text import html_to_text

MULTIPART = b"""\
From: "Dr. Jane Lee" <J.Lee@Uni.edu>
To: me@gmail.com, other@x.com
Cc: ta@uni.edu
Subject: =?utf-8?q?Thesis_review_=E2=80=94_Thursday=3F?=
Date: Mon, 28 Sep 2026 09:15:00 -0400
Message-ID: <abc123@uni.edu>
In-Reply-To: <prev@gmail.com>
References: <first@gmail.com> <prev@gmail.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b1"

--b1
Content-Type: text/plain; charset="utf-8"

Can you do Thursday 2pm?

Jane
--b1
Content-Type: text/html; charset="utf-8"

<p>Can you do <b>Thursday 2pm</b>?</p>
--b1--
"""

HTML_ONLY = b"""\
From: Chase <no-reply@chase.com>
To: me@gmail.com
Subject: Payment due
Date: Mon, 28 Sep 2026 10:00:00 +0000
Message-ID: <pay@chase.com>
Content-Type: text/html; charset="utf-8"

<html><head><style>p{color:red}</style></head><body><p>Your payment of&nbsp;$240</p><p>is due Oct&nbsp;3.</p></body></html>
"""


def test_gmail_multipart_prefers_plain_and_decodes_headers():
    meta = b"1 (X-GM-THRID 1790 X-GM-MSGID 1791 UID 5 FLAGS (\\Seen) BODY[] {123}"
    e = gmail.parse_message(MULTIPART, "me@gmail.com", meta)
    assert e.subject == "Thesis review — Thursday?"
    assert (e.sender_name, e.sender_addr) == ("Dr. Jane Lee", "j.lee@uni.edu")
    assert e.to == ["me@gmail.com", "other@x.com"] and e.cc == ["ta@uni.edu"]
    assert e.body_text == "Can you do Thursday 2pm?\n\nJane"
    assert (e.id, e.thread_id, e.is_read) == ("1791", "1790", True)
    assert e.message_id == "<abc123@uni.edu>"
    assert e.in_reply_to == "<prev@gmail.com>"
    assert e.references == ["<first@gmail.com>", "<prev@gmail.com>"]
    assert e.date == datetime(2026, 9, 28, 13, 15, tzinfo=timezone.utc)


def test_gmail_html_only_body_is_flattened_and_unread():
    e = gmail.parse_message(HTML_ONLY, "me@gmail.com", b"2 (X-GM-THRID 9 X-GM-MSGID 10 FLAGS ())")
    assert e.body_text == "Your payment of $240\n\nis due Oct 3."
    assert e.is_read is False


def test_gmail_without_gm_ids_falls_back_to_message_id():
    e = gmail.parse_message(HTML_ONLY, "me@gmail.com")
    assert e.id == e.thread_id == "<pay@chase.com>"


def test_html_to_text_drops_scripts_and_collapses_whitespace():
    assert html_to_text("<script>x()</script><div>a   b</div><br><br><br><div>c</div>") == "a b\n\nc"


def test_outlook_message_mapping():
    raw = {
        "id": "AAMk1",
        "conversationId": "conv1",
        "internetMessageId": "<m1@outlook.com>",
        "subject": " Contract ",
        "from": {"emailAddress": {"name": "Sam", "address": "Sam@Studio.co"}},
        "toRecipients": [{"emailAddress": {"name": "Me", "address": "me@hotmail.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-09-28T14:00:00Z",
        "body": {"contentType": "text", "content": "Final version attached.\r\n\r\n\r\nSam"},
        "isRead": False,
    }
    e = outlook.parse_message(raw, "me@hotmail.com")
    assert (e.provider, e.id, e.thread_id) == ("outlook", "AAMk1", "conv1")
    assert (e.sender_name, e.sender_addr, e.subject) == ("Sam", "sam@studio.co", "Contract")
    assert e.to == ["me@hotmail.com"]
    assert e.body_text == "Final version attached.\n\nSam"
    assert e.date == datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)


def test_mit_split_delivery_is_recognised_from_the_relay_hop():
    raw = (b"Received: from exchange-forwarding-east-3.mit.edu (exchange-forwarding-east-3.mit.edu. [18.9.21.14])\r\n"
           b" by mx.google.com with ESMTPS id x\r\n"
           b"Received: from mailman.mit.edu (18.7.21.50) by x.mail.protection.outlook.com\r\n"
           b"From: Willow <wrpicker@mit.edu>\r\nTo: sponge-talk@mit.edu\r\nSubject: Selling scooter\r\n"
           b"Date: Mon, 28 Sep 2026 20:00:00 +0000\r\nMessage-ID: <s@mit.edu>\r\n\r\nbody\r\n")
    assert gmail.parse_message(raw, "me@gmail.com").via == "mit"
    assert gmail.parse_message(HTML_ONLY, "me@gmail.com").via == ""
