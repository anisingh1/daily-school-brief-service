from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MESSAGE_POLL_CRON", "0 18 * * *")
    monkeypatch.setenv("MESSAGE_POLL_TIMEZONE", "Asia/Kolkata")
    monkeypatch.delenv("SERVICE_API_TOKEN", raising=False)
    monkeypatch.setattr(service.portal_archive, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(service.portal_archive, "ARCHIVE_PATH", tmp_path / "data" / "portal_messages.json")
    monkeypatch.setattr(service.portal_archive, "WHATSAPP_ARCHIVE_PATH", tmp_path / "data" / "whatsapp_messages.json")
    monkeypatch.setattr(service.portal_archive, "LAST_RUN_PATH", tmp_path / "data" / "last_run.json")
    monkeypatch.setattr(service.portal_archive, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(service.daily_brief, "OUTPUT_PATH", tmp_path / "output" / "daily_brief_input.json")
    monkeypatch.setattr(service.generate_brief, "INPUT_PATH", service.daily_brief.OUTPUT_PATH)
    monkeypatch.setattr(service.generate_brief, "CONTENT_PATH", tmp_path / "output" / "daily_brief_content.json")
    monkeypatch.setattr(service.send_email, "CONTENT_PATH", service.generate_brief.CONTENT_PATH)
    monkeypatch.setattr(service.send_email, "PREVIEW_PATH", tmp_path / "output" / "daily_brief_preview.html")
    monkeypatch.setattr(service.send_email, "send_brief_email", lambda *args: pytest.fail("Unexpected SMTP delivery"))
    monkeypatch.setattr(service.daily_brief, "fetch_recent_messages", lambda **kwargs: pytest.fail("Unexpected portal fetch"))
    monkeypatch.setattr(service.daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: pytest.fail("Unexpected IMAP fetch"))
    monkeypatch.setattr(service.generate_brief, "create_client", lambda: pytest.fail("Unexpected Azure call"))
    with TestClient(service.create_app()) as test_client:
        yield test_client


def test_favicon_returns_no_content_without_authentication(monkeypatch):
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-token")
    application = service.create_app()
    test_client = TestClient(application)
    response = test_client.get("/favicon.ico")
    assert response.status_code == 204
    assert response.content == b""
    assert "/favicon.ico" not in application.openapi()["paths"]


def test_pdf_links_serve_saved_files_without_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-token")
    monkeypatch.setattr(service.generate_brief, "PDF_DIR", tmp_path / "pdfs")
    service.generate_brief.PDF_DIR.mkdir()
    path = service.generate_brief.PDF_DIR / "Hindi worksheet.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    test_client = TestClient(service.create_app())
    assert test_client.get("/health").status_code == 401
    response = test_client.get("/pdfs/Hindi%20worksheet.pdf")
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize("filename", ["missing.pdf", "notice.txt", "outside.pdf"])
def test_pdf_links_reject_missing_non_pdf_and_symlink_files(client, tmp_path, monkeypatch, filename):
    directory = tmp_path / "pdfs"
    directory.mkdir()
    (directory / "notice.txt").write_text("Not a PDF")
    outside = tmp_path / "private.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    (directory / "outside.pdf").symlink_to(outside)
    monkeypatch.setattr(service.generate_brief, "PDF_DIR", directory)
    assert client.get(f"/pdfs/{filename}").status_code == 404
    assert client.get("/pdfs/%2E%2E%2Fprivate.pdf").status_code == 404


def test_scheduler_uses_env_cron_and_shuts_down(client):
    scheduler = client.app.state.scheduler
    job = scheduler.get_job("collect_messages")
    assert scheduler.running
    assert job.max_instances == 1
    assert job.coalesce
    now = datetime(2026, 10, 8, 12, tzinfo=service.portal_archive.IST)
    assert job.trigger.get_next_fire_time(None, now) == now.replace(hour=18)
    result = client.get("/health").json()
    assert result["cron"] == "0 18 * * *"
    assert result["timezone"] == "Asia/Kolkata"
    assert not result["initialized"]


@pytest.mark.parametrize(("hour", "expected"), [
    (0, "8 Oct 2026 12:00:00 AM"),
    (12, "8 Oct 2026 12:00:00 PM"),
    (22, "8 Oct 2026 10:00:00 PM"),
])
def test_poll_time_uses_twelve_hour_clock(hour, expected):
    timestamp = datetime(2026, 10, 8, hour, tzinfo=service.portal_archive.IST)
    assert service.format_poll_time(timestamp) == expected


def test_health_formats_next_poll_in_ist(client, monkeypatch):
    next_poll = datetime(2026, 10, 8, 4, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(client.app.state.scheduler, "get_job",
                        lambda job_id: SimpleNamespace(next_run_time=next_poll))
    result = client.get("/health").json()
    assert result["next_poll"] == "8 Oct 2026 10:00:00 AM"
    assert result["last_poll"] is None


@pytest.mark.parametrize("fails", [False, True])
def test_health_formats_last_poll_in_ist(client, monkeypatch, fails):
    fixed_now = datetime(2026, 10, 7, 20, 45, 12, tzinfo=timezone.utc)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    def collect():
        if fails:
            raise RuntimeError("Collection failed")
        return {}

    monkeypatch.setattr(service, "datetime", FixedDatetime)
    monkeypatch.setattr(service.daily_brief, "run", collect)
    client.app.state.scheduler.get_job("collect_messages").func()
    result = client.get("/health").json()
    assert result["last_poll"]["at"] == "8 Oct 2026 02:15:12 AM"
    assert ("error" in result["last_poll"]) is fails


@pytest.mark.parametrize(("route", "source"), [("/scrape-udt", "portal"), ("/fetch-whatsapp", "whatsapp")])
def test_source_endpoint_fetches_incrementally(client, monkeypatch, route, source):
    from datetime import timedelta

    previous = datetime.now(service.portal_archive.IST).replace(tzinfo=None) - timedelta(hours=1)
    service.portal_archive.save_last_run(previous)
    service.portal_archive.save_whatsapp_last_run(previous)
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs["cutoff"])
        return []

    fetch_name = "fetch_recent_messages" if source == "portal" else "fetch_recent_whatsapp_messages"
    monkeypatch.setattr(service.daily_brief, fetch_name, fetch)
    service.portal_archive.write_json(service.daily_brief.OUTPUT_PATH, {
        "portal": {"messages": [], "error": "previous portal failure"},
        "whatsapp": {"messages": [], "error": "previous whatsapp failure"},
    })
    response = client.post(route)
    assert response.status_code == 200
    assert response.json()["messages"] == {source: 0}
    expected = previous + timedelta(microseconds=1)
    if source == "whatsapp":
        expected = expected.replace(tzinfo=service.portal_archive.IST)
    assert calls == [expected]
    last_run = service.portal_archive.read_last_run if source == "portal" else service.portal_archive.read_whatsapp_last_run
    successful_run = last_run()
    assert successful_run is not None
    assert successful_run > previous
    assert client.post(route).status_code == 200
    expected = successful_run + timedelta(microseconds=1)
    if source == "whatsapp":
        expected = expected.replace(tzinfo=service.portal_archive.IST)
    assert calls[1] == expected
    assert service.daily_brief.OUTPUT_PATH.exists()
    import json

    saved = json.loads(service.daily_brief.OUTPUT_PATH.read_text())
    other_source = "whatsapp" if source == "portal" else "portal"
    assert saved[source]["error"] is None
    assert saved[other_source]["error"] == f"previous {other_source} failure"


@pytest.mark.parametrize(("route", "source"), [("/scrape-udt", "portal"), ("/fetch-whatsapp", "whatsapp")])
def test_source_endpoint_first_run_uses_retention(client, monkeypatch, route, source):
    cutoff = datetime(2026, 10, 8, 10, tzinfo=service.portal_archive.IST).replace(tzinfo=None)
    monkeypatch.setattr(service.portal_archive, "retention_cutoff", lambda *args: cutoff)
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs["cutoff"])
        return []

    fetch_name = "fetch_recent_messages" if source == "portal" else "fetch_recent_whatsapp_messages"
    monkeypatch.setattr(service.daily_brief, fetch_name, fetch)
    assert client.post(route).status_code == 200
    expected = cutoff if source == "portal" else cutoff.replace(tzinfo=service.portal_archive.IST)
    assert calls == [expected]


@pytest.mark.parametrize(("route", "source"), [("/scrape-udt", "portal"), ("/fetch-whatsapp", "whatsapp")])
def test_source_archive_failure_preserves_cursor(client, monkeypatch, route, source):
    previous = datetime(2026, 10, 8, 10, tzinfo=service.portal_archive.IST).replace(tzinfo=None)
    service.portal_archive.save_last_run(previous)
    service.portal_archive.save_whatsapp_last_run(previous)
    fetch_name = "fetch_recent_messages" if source == "portal" else "fetch_recent_whatsapp_messages"
    monkeypatch.setattr(service.daily_brief, fetch_name, lambda **kwargs: [])

    def fail_save(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(service.daily_brief, "save_archive", fail_save)
    monkeypatch.setattr(service.portal_archive, "save_archive", fail_save)
    assert client.post(route).status_code == 502
    assert service.portal_archive.read_last_run() == previous
    assert service.portal_archive.read_whatsapp_last_run() == previous


@pytest.mark.parametrize(("route", "source"), [("/scrape-udt", "portal"), ("/fetch-whatsapp", "whatsapp")])
def test_source_endpoint_failure_preserves_cursor(client, monkeypatch, route, source):
    previous = datetime(2026, 10, 8, 10, tzinfo=service.portal_archive.IST).replace(tzinfo=None)
    service.portal_archive.save_last_run(previous)
    service.portal_archive.save_whatsapp_last_run(previous)

    def fetch(**kwargs):
        raise RuntimeError("sensitive credential")

    fetch_name = "fetch_recent_messages" if source == "portal" else "fetch_recent_whatsapp_messages"
    monkeypatch.setattr(service.daily_brief, fetch_name, fetch)
    response = client.post(route)
    assert response.status_code == 502
    assert "sensitive" not in response.text
    assert service.portal_archive.read_last_run() == previous
    assert service.portal_archive.read_whatsapp_last_run() == previous
    assert not client.app.state.operation_lock.locked()


def test_email_requires_saved_brief(client):
    response = client.post("/email-brief")
    assert response.status_code == 409
    assert "POST /brief" in response.json()["detail"]
    assert not client.app.state.operation_lock.locked()


def test_email_sends_saved_brief_without_generation(client, monkeypatch):
    content = service.generate_brief.empty_brief("08 October 2026", [])
    content.homework = ["Read page 12"]
    service.generate_brief.write_content(content)
    calls = []
    monkeypatch.setattr(service.send_email, "send_brief_email", lambda subject, html: calls.append((subject, html)))
    monkeypatch.setattr(service.generate_brief, "generate", lambda **kwargs: pytest.fail("Unexpected brief generation"))
    response = client.post("/email-brief")
    assert response.status_code == 200
    assert response.json() == {"status": "sent"}
    assert len(calls) == 1
    assert calls[0][0] == "Daily School Brief - 08 October 2026"
    assert "Read page 12" in calls[0][1]
    assert service.send_email.PREVIEW_PATH.read_text() == calls[0][1]
    assert not client.app.state.operation_lock.locked()


@pytest.mark.parametrize("content", ["not json", '{"date": "missing fields"}'])
def test_email_rejects_invalid_saved_content(client, content):
    path = service.send_email.CONTENT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert client.post("/email-brief").status_code == 502
    assert not client.app.state.operation_lock.locked()


def test_email_failure_preserves_preview_and_releases_lock(client, monkeypatch):
    service.generate_brief.write_content(service.generate_brief.empty_brief("08 October 2026", []))
    calls = []

    def fail_delivery(*args):
        calls.append(args)
        raise RuntimeError("sensitive SMTP details")

    monkeypatch.setattr(service.send_email, "send_brief_email", fail_delivery)
    response = client.post("/email-brief")
    assert response.status_code == 502
    assert "sensitive" not in response.text
    assert len(calls) == 1
    assert service.send_email.PREVIEW_PATH.exists()
    assert service.send_email.CONTENT_PATH.exists()
    assert not client.app.state.operation_lock.locked()


def test_brief_requires_storage(client):
    response = client.post("/brief")
    assert response.status_code == 409
    assert "Initialize storage" in response.json()["detail"]


def test_brief_uses_stored_archives_without_fetching_or_sending(client, monkeypatch):
    message = {"id": "1", "posted_at": datetime.now(service.portal_archive.IST).isoformat(),
               "body": "Read page 12", "attachments": []}
    service.portal_archive.save_archive([message])
    calls = []

    def generate(data, now, **kwargs):
        assert not client.app.state.operation_lock.locked()
        calls.append(data)
        content = service.generate_brief.empty_brief("date", [])
        content.homework = ["Read page 12"]
        content.holidays = [service.generate_brief.Holiday.model_validate_json(
            '{"date": "2026-10-09", "reason": "School closure notice"}',
        )]
        content.calendar = service.generate_brief.normalize_calendar(content, now.date(), {})
        return content

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    monkeypatch.setattr(service.daily_brief.send_email, "run", lambda **kwargs: pytest.fail("Unexpected email"))
    response = client.post("/brief")
    assert response.status_code == 200
    assert response.json()["homework"] == ["Read page 12"]
    assert response.json()["holidays"] == [{"date": "2026-10-09", "reason": "School closure notice"}]
    assert len(response.json()["calendar"]) >= 28
    assert response.json()["calendar"][0]["date"].endswith("-01")
    assert calls[0]["portal"]["messages"] == [message]
    assert service.generate_brief.CONTENT_PATH.exists()
    saved = service.generate_brief.BriefContent.model_validate_json(
        service.generate_brief.CONTENT_PATH.read_text(encoding="utf-8"),
    )
    assert saved.model_dump(mode="json")["holidays"] == response.json()["holidays"]
    assert saved.model_dump(mode="json")["calendar"] == response.json()["calendar"]


def test_briefs_generate_in_parallel_while_poll_updates_storage(client, monkeypatch):
    timestamp = datetime.now(service.portal_archive.IST).isoformat()
    original = {"id": "old", "posted_at": timestamp, "body": "Old homework", "attachments": []}
    updated = {"id": "new", "posted_at": timestamp, "body": "New homework", "attachments": []}
    service.portal_archive.save_archive([original])
    first_started = Event()
    parallel_generation = Barrier(2)

    def generate(data, now, **kwargs):
        assert not client.app.state.operation_lock.locked()
        first_started.set()
        parallel_generation.wait(timeout=5)
        content = service.generate_brief.empty_brief("date", [])
        content.homework = [data["portal"]["messages"][0]["body"]]
        return content

    def collect():
        service.portal_archive.save_archive([updated])
        return {"portal": {"messages": [updated], "error": None}}

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    monkeypatch.setattr(service.daily_brief, "run", collect)
    monkeypatch.setattr(service, "prepare_storage", lambda data: {"messages": {"portal": 1}})
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(client.post, "/brief")
        assert first_started.wait(timeout=5)
        assert client.get("/health").json()["busy"] is False
        client.app.state.scheduler.get_job("collect_messages").func()
        assert client.get("/health").json()["last_poll"]["messages"] == {"portal": 1}
        second = client.post("/brief")
        first_response = first.result(timeout=5)
    assert first_response.status_code == second.status_code == 200
    assert first_response.json()["homework"] == ["Old homework"]
    assert second.json()["homework"] == ["New homework"]
    saved = service.generate_brief.BriefContent.model_validate_json(
        service.generate_brief.CONTENT_PATH.read_text(encoding="utf-8"),
    )
    assert saved.homework in (["Old homework"], ["New homework"])
    assert not client.app.state.operation_lock.locked()


def test_initialization_collects_all_and_prepares_json_without_brief(client, monkeypatch):
    calls = []
    data = {"portal": {"messages": [], "error": None}, "whatsapp": {"messages": [], "error": None}}
    monkeypatch.setattr(service.daily_brief, "run", lambda initialize: calls.append(initialize) or data)
    monkeypatch.setattr(service, "prepare_storage", lambda data: {"messages": {"portal": 0, "whatsapp": 0}, "pdf_documents": 0, "warnings": []})
    monkeypatch.setattr(service.generate_brief, "generate", lambda **kwargs: pytest.fail("Unexpected brief generation"))
    response = client.post("/initialize")
    assert response.status_code == 200
    assert calls == [True]


def test_poll_updates_both_archives_and_brief_reads_latest_storage(client, monkeypatch):
    timestamp = datetime.now(service.portal_archive.IST).replace(tzinfo=None).isoformat()
    portal = {"id": "new-portal", "posted_at": timestamp, "body": "New homework", "attachments": []}
    whatsapp = {"timestamp": timestamp, "text": "New notice", "attachments": []}
    monkeypatch.setattr(service.daily_brief, "fetch_recent_messages", lambda **kwargs: [portal])
    monkeypatch.setattr(service.daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [whatsapp])
    prepared = []
    monkeypatch.setattr(service, "prepare_storage", lambda data: prepared.append(data) or {
        "messages": {name: len(source["messages"]) for name, source in data.items()},
    })
    client.app.state.scheduler.get_job("collect_messages").func()
    assert prepared[0]["portal"]["messages"] == [portal]
    assert prepared[0]["whatsapp"]["messages"] == [whatsapp]
    assert service.portal_archive.read_last_run() is not None
    assert service.portal_archive.read_whatsapp_last_run() is not None
    assert client.get("/health").json()["last_poll"]["messages"] == {"portal": 1, "whatsapp": 1}
    service.portal_archive.write_json(service.daily_brief.OUTPUT_PATH, {
        "portal": {"messages": [], "error": None}, "whatsapp": {"messages": [], "error": None},
    })

    def generate(data, now, **kwargs):
        assert data["portal"]["messages"] == [portal]
        assert data["whatsapp"]["messages"] == [whatsapp]
        content = service.generate_brief.empty_brief("date", [])
        content.homework = ["New homework"]
        content.reminders = ["New notice"]
        return content

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    response = client.post("/brief")
    assert response.status_code == 200
    assert response.json()["homework"] == ["New homework"]
    assert response.json()["reminders"] == ["New notice"]


def test_scheduled_collection_prepares_storage_only(client, monkeypatch):
    calls = []
    data = {"portal": {"messages": [], "error": None}}
    monkeypatch.setattr(service.daily_brief, "run", lambda: calls.append("collect") or data)
    monkeypatch.setattr(service, "prepare_storage", lambda data: calls.append("cache") or {"messages": {"portal": 0}})
    monkeypatch.setattr(service.generate_brief, "generate", lambda **kwargs: pytest.fail("Unexpected brief generation"))
    client.app.state.scheduler.get_job("collect_messages").func()
    assert calls == ["collect", "cache"]
    assert client.get("/health").json()["last_poll"]["messages"] == {"portal": 0}


def test_busy_operations_are_rejected_and_polls_skip(client, monkeypatch):
    lock = client.app.state.operation_lock
    lock.acquire()
    try:
        assert client.post("/brief").status_code == 409
        assert client.post("/initialize").status_code == 409
        assert client.post("/scrape-udt").status_code == 409
        assert client.post("/fetch-whatsapp").status_code == 409
        assert client.post("/email-brief").status_code == 409
        monkeypatch.setattr(service.daily_brief, "run", lambda: pytest.fail("Busy poll must skip"))
        client.app.state.scheduler.get_job("collect_messages").func()
    finally:
        lock.release()


def test_preparing_storage_closes_client_and_caches_pdf(monkeypatch):
    data = {"portal": {"messages": [{"attachments": [{"saved_as": "planner.pdf"}]}], "error": None}}
    calls = []
    client = SimpleNamespace(close=lambda: calls.append("close"))
    monkeypatch.setattr(service.generate_brief, "create_client", lambda: (client, "deployment"))

    def prepare(source, **kwargs):
        assert source == data
        assert kwargs == {"client": client, "deployment": "deployment"}
        calls.append("extract")
        return {"documents": [{}]}, [], []

    monkeypatch.setattr(service.generate_brief, "prepare_input", prepare)
    assert service.prepare_storage(data)["pdf_documents"] == 1
    assert calls == ["extract", "close"]


def test_invalid_cron_is_rejected(monkeypatch):
    monkeypatch.setenv("MESSAGE_POLL_CRON", "invalid")
    with pytest.raises(ValueError, match="MESSAGE_POLL_CRON"):
        service.create_app()


def test_standard_cron_sunday_and_day_fields():
    now = datetime(2026, 10, 8, 12, tzinfo=service.portal_archive.IST)
    trigger = service.MessageCronTrigger("0 8 * * 0", service.portal_archive.IST)
    assert trigger.get_next_fire_time(None, now) == datetime(2026, 10, 11, 8, tzinfo=service.portal_archive.IST)
    trigger = service.MessageCronTrigger("0 8 15 * 0", service.portal_archive.IST)
    assert trigger.get_next_fire_time(None, now) == datetime(2026, 10, 11, 8, tzinfo=service.portal_archive.IST)


def test_shutdown_stops_scheduler(monkeypatch):
    monkeypatch.setenv("MESSAGE_POLL_CRON", "0 0 1 1 *")
    application = service.create_app()
    with TestClient(application):
        assert application.state.scheduler.running
    assert not application.state.scheduler.running


def test_initialization_failure_is_safe_and_releases_lock(client, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("password must not be returned")

    monkeypatch.setattr(service.daily_brief, "run", fail)
    response = client.post("/initialize")
    assert response.status_code == 502
    assert "password" not in response.text
    assert not client.app.state.operation_lock.locked()


def test_failed_portal_initialization_does_not_claim_success(client, monkeypatch):
    data = {"portal": {"messages": [], "error": "secret failure"}}
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: data)
    response = client.post("/initialize")
    assert response.status_code == 502
    assert "secret" not in response.text


def test_failed_brief_does_not_return_stale_content(client, monkeypatch):
    service.portal_archive.save_archive([])
    service.portal_archive.write_json(service.generate_brief.CONTENT_PATH, {"homework": ["stale"]})

    def fail(*args, **kwargs):
        raise RuntimeError("sensitive failure")

    monkeypatch.setattr(service.generate_brief, "generate", fail)
    response = client.post("/brief")
    assert response.status_code == 502
    assert "stale" not in response.text
    assert "sensitive" not in response.text
    assert not client.app.state.operation_lock.locked()


def test_failed_poll_releases_lock_and_can_retry(client, monkeypatch):
    def fail():
        raise RuntimeError("sensitive failure")

    monkeypatch.setattr(service.daily_brief, "run", fail)
    poll = client.app.state.scheduler.get_job("collect_messages").func
    poll()
    assert not client.app.state.operation_lock.locked()
    assert client.get("/health").json()["last_poll"]["error"] == "RuntimeError"
    monkeypatch.setattr(service.daily_brief, "run", dict)
    poll()
    assert "error" not in client.get("/health").json()["last_poll"]


def test_api_token_is_required_when_configured(monkeypatch):
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-token")
    monkeypatch.setenv("MESSAGE_POLL_CRON", "0 0 1 1 *")
    with TestClient(service.create_app()) as client:
        assert client.get("/health").status_code == 401
        assert client.post("/brief", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/initialize").status_code == 401
        assert client.post("/scrape-udt").status_code == 401
        assert client.post("/fetch-whatsapp").status_code == 401
        assert client.post("/email-brief").status_code == 401
        assert client.get("/health", headers={"Authorization": "Bearer test-token"}).status_code == 200


@pytest.mark.parametrize("reload_enabled", [False, True])
def test_service_run_configures_reload(monkeypatch, reload_enabled):
    monkeypatch.setenv("SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVICE_PORT", "8123")
    monkeypatch.setenv("SERVICE_RELOAD", "true" if reload_enabled else "false")
    calls = []
    monkeypatch.setattr(service.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    service.run()
    assert calls == [(("service:app" if reload_enabled else service.app,), {
        "host": "127.0.0.1", "port": 8123, "reload": reload_enabled,
    })]


def test_public_binding_requires_authentication(monkeypatch):
    monkeypatch.setenv("SERVICE_HOST", "0.0.0.0")
    monkeypatch.delenv("SERVICE_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SERVICE_API_TOKEN"):
        service.run()