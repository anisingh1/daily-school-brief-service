from datetime import datetime, timedelta
from email.message import EmailMessage

import pytest


@pytest.fixture(autouse=True)
def email_source(monkeypatch):
    monkeypatch.setenv("WHATSAPP_SOURCE", "email")


def test_parse_email_message_extracts_plain_text_message(tmp_path):
    from fetch_whatsapp import parse_email_message

    msg = EmailMessage()
    msg["Subject"] = "Vandana Arora_Aviraj Class Teacher"
    msg["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
    msg.set_content("Dear Parents\nGreetings!\nKindly check today's class work.")

    result = parse_email_message(msg, uid="101", save_dir=tmp_path)

    assert result == {
        "timestamp": "2026-09-03T13:09:00+05:30",
        "sender": "Vandana Arora_Aviraj Class Teacher",
        "text": "Dear Parents\nGreetings!\nKindly check today's class work.",
        "attachments": [],
    }


def test_parse_email_message_saves_pdf_attachment(tmp_path):
    from fetch_whatsapp import parse_email_message

    msg = EmailMessage()
    msg["Subject"] = "Vandana Arora_Aviraj Class Teacher"
    msg["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
    msg.set_content("")
    msg.add_attachment(
        b"%PDF-1.4 fake pdf bytes",
        maintype="application",
        subtype="pdf",
        filename="circular.pdf",
    )

    result = parse_email_message(msg, uid="202", save_dir=tmp_path)

    assert result["attachments"] == [
        {"name": "circular.pdf", "saved_as": str(tmp_path / "whatsapp_202_circular.pdf")}
    ]
    assert (tmp_path / "whatsapp_202_circular.pdf").read_bytes() == b"%PDF-1.4 fake pdf bytes"


@pytest.mark.parametrize("payload", [None, "not bytes", EmailMessage()])
def test_pdf_attachment_rejects_missing_or_non_byte_payload(tmp_path, monkeypatch, payload):
    import fetch_whatsapp

    part = EmailMessage()
    part["Content-Type"] = "application/pdf"
    part["Content-Disposition"] = 'attachment; filename="broken.pdf"'
    monkeypatch.setattr(part, "get_payload", lambda decode=False: payload)
    with pytest.raises(ValueError):
        fetch_whatsapp._save_pdf_attachment(part, "42", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("multipart", [False, True])
def test_text_message_rejects_non_byte_payload(monkeypatch, tmp_path, multipart):
    import fetch_whatsapp

    message = EmailMessage()
    message["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
    message.set_content("School notice")
    part = message
    if multipart:
        message.make_mixed()
        part = message.get_payload(0)
    monkeypatch.setattr(part, "get_payload", lambda decode=False: "not bytes")
    with pytest.raises(ValueError, match="non-byte decoded payload"):
        fetch_whatsapp.parse_email_message(message, "42", save_dir=tmp_path)


def test_parse_email_message_ignores_non_pdf_attachment(tmp_path):
    from fetch_whatsapp import parse_email_message

    msg = EmailMessage()
    msg["Subject"] = "Vandana Arora_Aviraj Class Teacher"
    msg["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
    msg.set_content("")
    msg.add_attachment(
        b"fake image bytes",
        maintype="image",
        subtype="jpeg",
        filename="photo.jpg",
    )

    result = parse_email_message(msg, uid="303", save_dir=tmp_path)

    assert result["attachments"] == []
    assert list(tmp_path.iterdir()) == []


def test_filter_recent_includes_message_within_window():
    from fetch_whatsapp import filter_recent

    now = datetime.now().astimezone()
    recent = (now - timedelta(hours=1)).isoformat()
    messages = [{"timestamp": recent, "sender": "A", "text": "hi"}]
    result = filter_recent(messages, lookback_hours=36)
    assert len(result) == 1


def test_filter_recent_excludes_message_before_cutoff():
    from fetch_whatsapp import filter_recent

    now = datetime.now().astimezone()
    old = (now - timedelta(hours=40)).isoformat()
    messages = [{"timestamp": old, "sender": "A", "text": "hi"}]
    result = filter_recent(messages, lookback_hours=36)
    assert result == []


def test_filter_recent_skips_entries_without_timestamp():
    from fetch_whatsapp import filter_recent

    messages = [{"sender": "A", "text": "no timestamp field"}]
    result = filter_recent(messages, lookback_hours=36)
    assert result == []


def test_filter_recent_includes_naive_timestamp_within_window():
    from fetch_whatsapp import filter_recent

    # No .astimezone(): produces a naive (no UTC offset) ISO string, which is
    # plausibly what the phone-side automation actually emits. This exercises
    # the "if ts.tzinfo is None" branch that tz-aware timestamps never hit.
    recent_naive = (datetime.now() - timedelta(hours=1)).isoformat()
    messages = [{"timestamp": recent_naive, "sender": "A", "text": "hi"}]
    result = filter_recent(messages, lookback_hours=36)
    assert len(result) == 1


def test_filter_recent_sorts_chronologically_not_lexicographically():
    from fetch_whatsapp import filter_recent

    # Non-zero-padded hour/minute/second: "9:5:0" sorts *after* "10:00:00"
    # lexicographically, even though it is chronologically earlier (09:05:00
    # < 10:00:00). This would fail if filter_recent sorted by the raw
    # timestamp string instead of the parsed datetime. Both timestamps use
    # "today" (same date as now) and the local UTC offset so they fall
    # inside the lookback window regardless of the current time of day.
    now = datetime.now().astimezone()
    date_str = now.strftime("%Y-%m-%d")
    offset = now.strftime("%z")
    offset_str = f"{offset[:3]}:{offset[3:]}" if offset else ""
    earlier = f"{date_str}T9:5:0{offset_str}"
    later = f"{date_str}T10:00:00{offset_str}"
    assert earlier > later  # sanity check: lexicographic order is reversed

    messages = [
        {"timestamp": later, "sender": "A", "text": "second"},
        {"timestamp": earlier, "sender": "B", "text": "first"},
    ]
    result = filter_recent(messages, lookback_hours=24)
    assert [m["text"] for m in result] == ["first", "second"]


def test_filter_recent_skips_entry_with_overflow_timestamp():
    from fetch_whatsapp import filter_recent

    messages = [{"timestamp": "99999999999999999999", "sender": "A", "text": "bad"}]
    result = filter_recent(messages, lookback_hours=36)
    assert result == []


def test_fetch_recent_whatsapp_messages_parses_imap_search_results(monkeypatch, tmp_path):
    import fetch_whatsapp

    msg = EmailMessage()
    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_SUBJECT_PREFIX", "[WhatsApp School]")
    msg["Subject"] = "[WhatsApp School] Vandana Arora_Aviraj Class Teacher"
    msg["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
    msg.set_content("Kindly check today's class work.")
    raw = msg.as_bytes()

    class FakeConn:
        def __init__(self):
            self.logged_out = False

        def search(self, charset, *criteria):
            return "OK", [b"42"]

        def fetch(self, uid, parts):
            assert uid == b"42"
            return "OK", [None, b"metadata", (b"42 (RFC822 {n}", raw), b")"]

        def logout(self):
            self.logged_out = True

    fake_conn = FakeConn()
    monkeypatch.setattr(fetch_whatsapp, "_connect_imap", lambda: fake_conn)
    monkeypatch.setattr(fetch_whatsapp, "ATTACHMENTS_DIR", tmp_path)

    cutoff = datetime(2026, 9, 1).astimezone()
    result = fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=cutoff)

    assert len(result) == 1
    assert result[0]["sender"] == "Vandana Arora_Aviraj Class Teacher"
    assert result[0]["text"] == "Kindly check today's class work."
    assert fake_conn.logged_out is True


@pytest.mark.parametrize("fetch_status, fetch_data", [
    ("NO", [None]), ("OK", []), ("OK", [None]), ("OK", [b")"]),
    ("OK", [(b"RFC822", None)]),
])
def test_invalid_imap_fetch_fails_and_logs_out(monkeypatch, fetch_status, fetch_data):
    import fetch_whatsapp

    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_SUBJECT_PREFIX", "[WhatsApp School]")

    class FakeConn:
        logged_out = False

        def search(self, charset, *criteria):
            return "OK", [b"42"]

        def fetch(self, uid, parts):
            return fetch_status, fetch_data

        def logout(self):
            self.logged_out = True

    connection = FakeConn()
    monkeypatch.setattr(fetch_whatsapp, "_connect_imap", lambda: connection)
    with pytest.raises(RuntimeError, match="IMAP"):
        fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=datetime.fromisoformat("2026-09-01T12:00:00+05:30"))
    assert connection.logged_out


def test_fetch_skips_attachment_parsing_before_exact_cutoff(monkeypatch):
    import fetch_whatsapp

    msg = EmailMessage()
    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_SUBJECT_PREFIX", "[WhatsApp School]")
    msg["Subject"] = "[WhatsApp School] Old notice"
    msg["Date"] = "Tue, 01 Sep 2026 10:00:00 +0530"
    msg.set_content("Old notice")
    msg.add_attachment(b"old pdf", maintype="application", subtype="pdf", filename="old.pdf")

    class FakeConn:
        def search(self, charset, *criteria):
            return "OK", [b"42"]

        def fetch(self, uid, parts):
            return "OK", [(b"42 (RFC822 {n}", msg.as_bytes())]

        def logout(self):
            pass

    def unexpected_parse(*args):
        raise AssertionError("Older email must not parse or save attachments")

    monkeypatch.setattr(fetch_whatsapp, "_connect_imap", FakeConn)
    monkeypatch.setattr(fetch_whatsapp, "parse_email_message", unexpected_parse)
    cutoff = datetime.fromisoformat("2026-09-01T12:00:00+05:30")
    assert fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=cutoff) == []


@pytest.mark.parametrize("prefix", ["[WhatsApp School]", "[School WhatsApp]"])
def test_fetch_rejects_unmarked_emails_and_decodes_marked_subjects(monkeypatch, tmp_path, prefix):
    import fetch_whatsapp

    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_SUBJECT_PREFIX", prefix)
    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_EMAIL_TO", "capture@example.com")
    emails = {}
    for index, subject in enumerate([
        f"{prefix} Class Teacher \u00e9", "Newsletter", f"Fwd: {prefix} Teacher",
        f"{prefix}Unexpected", "", "[Other Source] Notice",
    ]):
        message = EmailMessage()
        if subject:
            message["Subject"] = subject
        if index == 0:
            message["Date"] = "Thu, 03 Sep 2026 13:09:00 +0530"
            message.set_content("School notice")
        else:
            message.set_content("Unrelated email without a valid date")
            message.add_attachment(b"unrelated pdf", maintype="application", subtype="pdf", filename="unrelated.pdf")
        emails[str(index).encode()] = message.as_bytes()

    class FakeConn:
        logged_out = False

        def search(self, charset, *criteria):
            assert criteria == ("TO", '"capture@example.com"', "SINCE", "01-Sep-2026")
            return "OK", [b" ".join(emails)]

        def fetch(self, uid, parts):
            return "OK", [(b"RFC822", emails[uid])]

        def logout(self):
            self.logged_out = True

    fake_conn = FakeConn()
    original_parse = fetch_whatsapp.parse_email_message
    parsed_uids = []

    def parse(message, uid):
        parsed_uids.append(uid)
        return original_parse(message, uid, save_dir=tmp_path)

    monkeypatch.setattr(fetch_whatsapp, "_connect_imap", lambda: fake_conn)
    monkeypatch.setattr(fetch_whatsapp, "parse_email_message", parse)
    result = fetch_whatsapp.fetch_recent_whatsapp_messages(
        cutoff=datetime.fromisoformat("2026-09-01T12:00:00+05:30"),
    )
    assert len(result) == 1
    assert result[0]["sender"] == "Class Teacher \u00e9"
    assert result[0]["text"] == "School notice"
    assert parsed_uids == ["0"]
    assert list(tmp_path.iterdir()) == []
    assert fake_conn.logged_out


def test_empty_capture_marker_is_rejected_before_connecting(monkeypatch):
    import fetch_whatsapp

    monkeypatch.setattr(fetch_whatsapp, "WHATSAPP_SUBJECT_PREFIX", "")
    monkeypatch.setattr(fetch_whatsapp, "_connect_imap", lambda: pytest.fail("Must not connect without a capture marker"))
    with pytest.raises(RuntimeError, match="WHATSAPP_SUBJECT_PREFIX must not be empty"):
        fetch_whatsapp.fetch_recent_whatsapp_messages()


def test_filter_recent_uses_explicit_cutoff_over_lookback_hours():
    from fetch_whatsapp import filter_recent

    cutoff = datetime(2026, 9, 4, 12, 0, 0).astimezone()
    just_after = (cutoff + timedelta(minutes=1)).isoformat()
    just_before = (cutoff - timedelta(minutes=1)).isoformat()
    messages = [
        {"timestamp": just_after, "sender": "A", "text": "in"},
        {"timestamp": just_before, "sender": "A", "text": "out"},
    ]
    # lookback_hours=1000 would normally include both if it were used -
    # confirming cutoff, not lookback_hours, governs the result.
    result = filter_recent(messages, lookback_hours=1000, cutoff=cutoff)
    assert [m["text"] for m in result] == ["in"]


def test_filter_recent_normalizes_naive_explicit_cutoff():
    from fetch_whatsapp import filter_recent

    naive_cutoff = datetime(2026, 9, 4, 12, 0, 0)  # no tzinfo - simulates cutoff.compute_cutoff()
    just_after = (naive_cutoff + timedelta(minutes=1)).isoformat()  # also naive
    messages = [{"timestamp": just_after, "sender": "A", "text": "hi"}]

    result = filter_recent(messages, cutoff=naive_cutoff)  # must not raise TypeError

    assert len(result) == 1
