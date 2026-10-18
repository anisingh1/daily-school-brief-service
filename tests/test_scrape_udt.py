from datetime import datetime, timedelta
from pathlib import Path

import pytest


def test_fetch_attendance_normalizes_status_and_filters_dates(monkeypatch):
    import scrape_udt
    from unittest.mock import MagicMock

    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.json.return_value = [
        {"date": "2026-10-14", "title": "Present"},
        {"date": "2026-10-15", "title": "Absent"},
        {"date": "2026-10-31", "title": "leave"},
    ]
    monkeypatch.setattr(scrape_udt.requests, "Session", lambda: session)
    monkeypatch.setattr(scrape_udt, "login", lambda session: None)
    assert scrape_udt.fetch_attendance(datetime(2026, 10, 1), datetime(2026, 10, 15)) == [
        {"date": "2026-10-14", "status": "present"},
        {"date": "2026-10-15", "status": "absent"},
    ]
    assert session.get.call_args.kwargs["params"] == {"year": 2026, "month": 10}


def test_fetch_attendance_fetches_each_month_across_year_boundary(monkeypatch):
    import scrape_udt
    from unittest.mock import MagicMock

    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.json.return_value = []
    monkeypatch.setattr(scrape_udt.requests, "Session", lambda: session)
    monkeypatch.setattr(scrape_udt, "login", lambda session: None)
    assert scrape_udt.fetch_attendance(datetime(2026, 12, 20), datetime(2027, 1, 5)) == []
    assert [call.kwargs["params"] for call in session.get.call_args_list] == [
        {"year": 2026, "month": 12}, {"year": 2027, "month": 1},
    ]


def _msg(offset_hours, msg_id):
    posted = datetime.now() - timedelta(hours=offset_hours)
    return {
        "id": msg_id,
        "title": "t",
        "posted_at": posted.isoformat(),
        "body": "b",
        "attachments": [],
    }


def test_filter_recent_includes_message_within_window():
    from scrape_udt import filter_recent

    messages = [_msg(1, "recent")]
    result = filter_recent(messages, lookback_hours=36)
    assert len(result) == 1


def test_filter_recent_excludes_message_before_cutoff():
    from scrape_udt import filter_recent

    messages = [_msg(40, "old")]
    result = filter_recent(messages, lookback_hours=36)
    assert result == []


def test_filter_recent_sorts_most_recent_first():
    from scrape_udt import filter_recent

    messages = [_msg(30, "a"), _msg(2, "b"), _msg(10, "c")]
    result = filter_recent(messages, lookback_hours=36)
    assert [m["id"] for m in result] == ["b", "c", "a"]


def test_filter_recent_uses_explicit_cutoff_over_lookback_hours():
    from scrape_udt import filter_recent

    cutoff = datetime(2026, 9, 4, 12, 0, 0)
    messages = [
        {"id": "in", "title": "t", "posted_at": (cutoff + timedelta(hours=1)).isoformat(), "body": "b", "attachments": []},
        {"id": "out", "title": "t", "posted_at": (cutoff - timedelta(hours=1)).isoformat(), "body": "b", "attachments": []},
    ]
    # lookback_hours=1000 would normally include both if it were used -
    # confirming cutoff, not lookback_hours, governs the result.
    result = filter_recent(messages, lookback_hours=1000, cutoff=cutoff)
    assert [m["id"] for m in result] == ["in"]


def test_login_raises_on_failed_login():
    from scrape_udt import login

    class FakeResponse:
        text = '<div id="login-form"></div>'

        def raise_for_status(self):
            pass

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    with pytest.raises(RuntimeError):
        login(FakeSession())


@pytest.mark.parametrize(("url", "text", "succeeds"), [
    ("https://portal.test/user-dash-boards/parent", "<html>Parent dashboard</html>", True),
    ("https://portal.test/user-dash-boards/parent/", "<html>Parent dashboard</html>", True),
    ("https://portal.test/Logins/index", "<html>Login unavailable</html>", False),
    ("https://other.test/user-dash-boards/parent", "<html>Dashboard</html>", False),
    ("https://portal.test/user-dash-boards/parent", "<form id='login-form'></form>", False),
])
def test_connection_probe_validates_login_without_fetching_messages(monkeypatch, url, text, succeeds):
    from types import SimpleNamespace
    import scrape_udt

    calls = []
    response = SimpleNamespace(url=url, text=text, raise_for_status=lambda: None)

    class Session:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("close")

        def post(self, target, **kwargs):
            calls.append(("login", target, kwargs["timeout"]))
            return response

        def get(self, *args, **kwargs):
            pytest.fail("Connection probe must not collect messages or attachments")

    monkeypatch.setattr(scrape_udt, "BASE_URL", "https://portal.test")
    monkeypatch.setattr(scrape_udt, "LOGIN_URL", "https://portal.test/Logins/index")
    monkeypatch.setattr(scrape_udt, "USERNAME", "test-user")
    monkeypatch.setattr(scrape_udt, "PASSWORD", "test-password")
    monkeypatch.setattr(scrape_udt.requests, "Session", Session)
    if succeeds:
        assert scrape_udt.check_connection() is None
    else:
        with pytest.raises(RuntimeError):
            scrape_udt.check_connection()
    assert calls == [("login", "https://portal.test/Logins/index", 10), "close"]


class _FakeSession:
    def __init__(self):
        self.headers = {}


class _FakeRequests:
    """Stand-in for the `requests` module, patched onto scrape_udt so
    fetch_recent_messages() gets our fake session instead of a real one."""

    def __init__(self, session):
        self._session = session

    def Session(self):
        return self._session


def test_fetch_recent_messages_raises_on_login_failure(monkeypatch):
    import scrape_udt

    fake_session = _FakeSession()
    monkeypatch.setattr(scrape_udt, "requests", _FakeRequests(fake_session))

    def fake_login(session):
        raise RuntimeError("login failed")

    monkeypatch.setattr(scrape_udt, "login", fake_login)

    with pytest.raises(RuntimeError):
        scrape_udt.fetch_recent_messages(lookback_hours=36)


def test_fetch_recent_messages_returns_filtered_without_downloading_by_default(monkeypatch):
    import scrape_udt

    fake_session = _FakeSession()
    monkeypatch.setattr(scrape_udt, "requests", _FakeRequests(fake_session))
    monkeypatch.setattr(scrape_udt, "login", lambda session: None)
    monkeypatch.setattr(
        scrape_udt,
        "fetch_activity_messages",
        lambda session: [_msg(1, "recent"), _msg(40, "old")],
    )

    download_calls = []
    monkeypatch.setattr(
        scrape_udt,
        "download_message_attachments",
        lambda session, messages: download_calls.append(messages),
    )

    result = scrape_udt.fetch_recent_messages(lookback_hours=36)

    assert [m["id"] for m in result] == ["recent"]
    assert download_calls == []


def test_fetch_recent_messages_downloads_attachments_when_requested(monkeypatch):
    import scrape_udt

    fake_session = _FakeSession()
    monkeypatch.setattr(scrape_udt, "requests", _FakeRequests(fake_session))
    monkeypatch.setattr(scrape_udt, "login", lambda session: None)
    monkeypatch.setattr(
        scrape_udt,
        "fetch_activity_messages",
        lambda session: [_msg(1, "recent")],
    )

    download_calls = []
    monkeypatch.setattr(
        scrape_udt,
        "download_message_attachments",
        lambda session, messages: download_calls.append((session, messages)),
    )

    result = scrape_udt.fetch_recent_messages(lookback_hours=36, download_attachments=True)

    assert len(download_calls) == 1
    called_session, called_messages = download_calls[0]
    assert called_session is fake_session
    assert called_messages == result


def test_download_message_attachments_skips_already_downloaded_file(tmp_path, monkeypatch):
    import scrape_udt

    monkeypatch.setattr(scrape_udt, "PDF_DIR", tmp_path)
    existing_file = tmp_path / "msg1_Homework.pdf"
    existing_file.write_bytes(b"old content")

    class FakeViewerResponse:
        text = 'var file_path = "http://example.com/files/homework.pdf";'

    class FakeSession:
        def __init__(self):
            self.get_calls = []

        def get(self, url):
            self.get_calls.append(url)
            return FakeViewerResponse()

    session = FakeSession()
    messages = [{"id": "msg1", "attachments": [{"name": "Homework", "href": "/viewer/1"}]}]

    scrape_udt.download_message_attachments(session, messages)

    assert messages[0]["attachments"][0]["saved_as"] == str(existing_file)
    assert messages[0]["attachments"][0]["note"] == "Already downloaded"
    assert existing_file.read_bytes() == b"old content"
    assert len(session.get_calls) == 1


def test_download_message_attachments_downloads_new_file(tmp_path, monkeypatch):
    import scrape_udt

    monkeypatch.setattr(scrape_udt, "PDF_DIR", tmp_path)

    class FakeViewerResponse:
        text = 'var file_path = "http://example.com/files/homework.pdf";'

    class FakeFileResponse:
        content = b"pdf bytes"

    class FakeSession:
        def get(self, url):
            if "viewer" in url:
                return FakeViewerResponse()
            return FakeFileResponse()

    messages = [{"id": "msg1", "attachments": [{"name": "Homework", "href": "/viewer/1"}]}]

    scrape_udt.download_message_attachments(FakeSession(), messages)

    saved_path = tmp_path / "msg1_Homework.pdf"
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"pdf bytes"
    assert messages[0]["attachments"][0]["saved_as"] == str(saved_path)


def test_download_message_attachments_strips_query_string_from_extension(tmp_path, monkeypatch):
    import scrape_udt

    monkeypatch.setattr(scrape_udt, "PDF_DIR", tmp_path)

    class FakeViewerResponse:
        text = 'var file_path = "http://example.com/files/homework.pdf?token=abc123";'

    class FakeFileResponse:
        content = b"pdf bytes"

    class FakeSession:
        def get(self, url):
            if "viewer" in url:
                return FakeViewerResponse()
            return FakeFileResponse()

    messages = [{"id": "msg1", "attachments": [{"name": "Homework", "href": "/viewer/1"}]}]

    scrape_udt.download_message_attachments(FakeSession(), messages)

    saved_as = messages[0]["attachments"][0]["saved_as"]
    assert saved_as.endswith(".pdf")
    assert "?" not in saved_as
    assert Path(saved_as).read_bytes() == b"pdf bytes"


def test_download_message_attachments_does_not_collide_across_messages_with_same_name(tmp_path, monkeypatch):
    import scrape_udt

    monkeypatch.setattr(scrape_udt, "PDF_DIR", tmp_path)

    class FakeSession:
        def get(self, url):
            if "viewer1" in url:
                return type("R", (), {"text": 'var file_path = "http://example.com/files/day1.pdf";'})()
            if "viewer2" in url:
                return type("R", (), {"text": 'var file_path = "http://example.com/files/day2.pdf";'})()
            if "day1.pdf" in url:
                return type("R", (), {"content": b"day one content"})()
            if "day2.pdf" in url:
                return type("R", (), {"content": b"day two content"})()
            raise AssertionError(f"unexpected url: {url}")

    messages = [
        {"id": "msg1", "attachments": [{"name": "Homework", "href": "/viewer1"}]},
        {"id": "msg2", "attachments": [{"name": "Homework", "href": "/viewer2"}]},
    ]

    scrape_udt.download_message_attachments(FakeSession(), messages)

    saved1 = messages[0]["attachments"][0]["saved_as"]
    saved2 = messages[1]["attachments"][0]["saved_as"]

    assert saved1 != saved2
    assert Path(saved1).read_bytes() == b"day one content"
    assert Path(saved2).read_bytes() == b"day two content"


def test_download_message_attachments_does_not_collide_when_both_ids_empty(tmp_path, monkeypatch):
    import scrape_udt

    monkeypatch.setattr(scrape_udt, "PDF_DIR", tmp_path)

    class FakeSession:
        def get(self, url):
            if "viewer1" in url:
                return type("R", (), {"text": 'var file_path = "http://example.com/files/day1.pdf";'})()
            if "viewer2" in url:
                return type("R", (), {"text": 'var file_path = "http://example.com/files/day2.pdf";'})()
            if "day1.pdf" in url:
                return type("R", (), {"content": b"day one content"})()
            if "day2.pdf" in url:
                return type("R", (), {"content": b"day two content"})()
            raise AssertionError(f"unexpected url: {url}")

    messages = [
        {"id": "", "attachments": [{"name": "Homework", "href": "/viewer1"}]},
        {"id": "", "attachments": [{"name": "Homework", "href": "/viewer2"}]},
    ]

    scrape_udt.download_message_attachments(FakeSession(), messages)

    saved1 = messages[0]["attachments"][0]["saved_as"]
    saved2 = messages[1]["attachments"][0]["saved_as"]

    assert saved1 != saved2
    assert Path(saved1).read_bytes() == b"day one content"
    assert Path(saved2).read_bytes() == b"day two content"
