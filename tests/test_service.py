from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service.daily_brief, "fetch_attendance", lambda *args: [])
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
    monkeypatch.setattr(service.daily_brief, "fetch_recent_messages", lambda **kwargs: pytest.fail("Unexpected portal fetch"))
    monkeypatch.setattr(service.daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: pytest.fail("Unexpected Evolution fetch"))
    monkeypatch.setattr(service.generate_brief, "create_client", lambda: pytest.fail("Unexpected Azure call"))
    with TestClient(service.create_app()) as test_client:
        yield test_client


def test_whatsapp_collection_reports_unavailable_pdf_warnings(client, monkeypatch):
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: {
        "whatsapp": {"messages": [{
            "text": "Notice", "attachments": [{
                "name": "notice.pdf", "download_error": "Download failed; will retry.",
            }],
        }], "error": None},
    })
    response = client.post("/fetch-whatsapp")
    assert response.status_code == 200
    assert response.json()["messages"] == {"whatsapp": 1}
    assert response.json()["warnings"] == ["whatsapp: notice.pdf: Download failed; will retry."]


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


def test_email_endpoint_is_removed(client):
    assert "/email-brief" not in client.app.openapi()["paths"]
    assert client.post("/email-brief").status_code == 404


def test_brief_requires_saved_content(client):
    response = client.post("/brief")
    assert response.status_code == 409
    assert "POST /refresh" in response.json()["detail"]
    assert client.get("/brief").status_code == 409


def test_brief_reads_saved_content_without_fetching_or_generating(client, monkeypatch):
    content = service.generate_brief.empty_brief("date", [])
    content.homework = ["Read page 12"]
    content.holidays = [service.generate_brief.Holiday.model_validate_json(
        '{"date": "2026-10-09", "reason": "School closure notice"}',
    )]
    content.calendar = service.generate_brief.normalize_calendar(
        content, datetime(2026, 10, 8).date(), {},
    )
    service.generate_brief.write_content(content)
    monkeypatch.setattr(service.daily_brief, "run", lambda: pytest.fail("Unexpected collection"))
    monkeypatch.setattr(service.generate_brief, "generate", lambda *args, **kwargs: pytest.fail("Unexpected generation"))
    monkeypatch.setattr(service.generate_brief, "load_stored_input", lambda *args: pytest.fail("Unexpected archive loading"))
    with client.app.state.operation_lock:
        for request in (client.get, client.post):
            response = request("/brief")
            assert response.status_code == 200
            assert response.json() == content.model_dump(mode="json", exclude={"warnings"})


def test_saved_brief_reads_remain_available_during_refresh(client, monkeypatch):
    timestamp = datetime.now(service.portal_archive.IST).isoformat()
    updated = {"id": "new", "posted_at": timestamp, "body": "New homework", "attachments": []}
    original = service.generate_brief.empty_brief("date", [])
    original.homework = ["Old homework"]
    service.generate_brief.write_content(original)
    generation_started = Event()
    finish_generation = Event()
    calls = []

    def generate(data, now, **kwargs):
        assert client.app.state.operation_lock.locked()
        calls.append("generate")
        generation_started.set()
        assert finish_generation.wait(timeout=5)
        content = service.generate_brief.empty_brief("date", [])
        content.homework = [data["portal"]["messages"][0]["body"]]
        return content

    def collect():
        calls.append("collect")
        service.portal_archive.save_archive([updated])
        return {"portal": {"messages": [updated], "error": None}}

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    monkeypatch.setattr(service.daily_brief, "run", collect)
    monkeypatch.setattr(service, "prepare_storage", lambda data: calls.append("cache") or {"messages": {"portal": 1}})
    with ThreadPoolExecutor(max_workers=1) as executor:
        refresh = executor.submit(client.post, "/refresh")
        try:
            assert generation_started.wait(timeout=5)
            assert client.get("/health").json()["busy"] is True
            for request in (client.get, client.post):
                response = request("/brief")
                assert response.status_code == 200
                assert response.json()["homework"] == ["Old homework"]
            assert client.post("/refresh").status_code == 409
            client.app.state.scheduler.get_job("collect_messages").func()
            assert calls == ["collect", "cache", "generate"]
        finally:
            finish_generation.set()
        assert refresh.result(timeout=5).status_code == 200
    assert client.get("/brief").json()["homework"] == ["New homework"]
    assert client.get("/health").json()["last_poll"]["brief_generated"] is True
    assert not client.app.state.operation_lock.locked()


def test_saved_legacy_highlight_is_returned_as_reminder(client):
    content = service.generate_brief.empty_brief("date", []).model_dump(mode="json")
    content["aviraj_highlight"] = "Aviraj: bring an assembly prop tomorrow"
    service.portal_archive.write_json(service.generate_brief.CONTENT_PATH, content)
    for request in (client.get, client.post):
        response = request("/brief")
        assert response.status_code == 200
        assert "aviraj_highlight" not in response.json()
        assert response.json()["reminders"] == [content["aviraj_highlight"]]


def test_initialization_collects_all_and_prepares_json_without_brief(client, monkeypatch):
    calls = []
    data = {"portal": {"messages": [], "error": None}, "whatsapp": {"messages": [], "error": None}}
    monkeypatch.setattr(service.daily_brief, "run", lambda initialize: calls.append(initialize) or data)
    monkeypatch.setattr(service, "prepare_storage", lambda data: {
        "messages": {"portal": 0, "whatsapp": 0}, "pdf_documents": 0,
        "warnings": ["Initialization warning"],
    })
    monkeypatch.setattr(service.generate_brief, "generate", lambda **kwargs: pytest.fail("Unexpected brief generation"))
    response = client.post("/initialize")
    assert response.status_code == 200
    assert response.json()["warnings"] == ["Initialization warning"]
    assert calls == [True]


def test_initialize_and_whatsapp_fetch_only_transcribe_uncached_pdfs(client, monkeypatch, tmp_path):
    import json
    import pymupdf

    monkeypatch.setattr(service.generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(service.generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    service.generate_brief.PDF_DIR.mkdir(parents=True)
    path = service.generate_brief.PDF_DIR / "worksheet.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Read page 12")
        document.save(path)
    data = {
        "portal": {"messages": [], "error": None},
        "whatsapp": {"messages": [{"attachments": [{"saved_as": str(path)}]}], "error": None},
    }
    calls = []

    def create(**kwargs):
        calls.append(kwargs["response_format"]["json_schema"]["name"])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=json.dumps({
                "category": "notebook_page", "pages": [{"page": 1, "text": "Read page 12"}], "warnings": [],
            })),
        )])

    azure = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(service.generate_brief, "create_client", lambda: (azure, "deployment"))
    monkeypatch.setattr(service.generate_brief, "generate", lambda *args, **kwargs: pytest.fail("Unexpected final generation"))
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: data)
    for route in ("/initialize", "/fetch-whatsapp"):
        response = client.post(route)
        assert response.status_code == 200
        assert response.json()["pdf_documents"] == 1
    assert calls == ["pdf_text"]
    assert not service.generate_brief.CONTENT_PATH.exists()


@pytest.mark.parametrize(("endpoint", "source"), [
    ("/scrape-udt", "portal"), ("/fetch-whatsapp", "whatsapp"),
])
def test_source_collection_responses_omit_warnings(client, monkeypatch, endpoint, source):
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: {
        source: {"messages": [], "error": None},
    })
    monkeypatch.setattr(service, "prepare_storage", lambda data: {
        "messages": {source: 0}, "pdf_documents": 0, "warnings": ["PDF warning"],
    })
    response = client.post(endpoint)
    assert response.status_code == 200
    assert response.json() == {"messages": {source: 0}, "pdf_documents": 0}


def test_poll_updates_both_archives_and_brief_reads_latest_storage(client, monkeypatch):
    timestamp = datetime.now(service.portal_archive.IST).replace(tzinfo=None).isoformat()
    portal = {"id": "new-portal", "posted_at": timestamp, "body": "New homework", "attachments": []}
    whatsapp = {"timestamp": timestamp, "text": "New notice", "attachments": []}
    monkeypatch.setattr(service.daily_brief, "fetch_recent_messages", lambda **kwargs: [portal])
    monkeypatch.setattr(service.daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [whatsapp])
    prepared = []
    prepare_storage = service.prepare_storage
    monkeypatch.setattr(service, "prepare_storage", lambda data: prepared.append(data) or prepare_storage(data))
    def generate(data, now, **kwargs):
        assert data["portal"]["messages"] == [portal]
        assert data["whatsapp"]["messages"] == [whatsapp]
        content = service.generate_brief.empty_brief("date", [])
        content.homework = ["New homework"]
        content.reminders = ["New notice"]
        return content

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    client.app.state.scheduler.get_job("collect_messages").func()
    assert prepared[0]["portal"]["messages"] == [portal]
    assert prepared[0]["whatsapp"]["messages"] == [whatsapp]
    assert service.portal_archive.read_last_run() is not None
    assert service.portal_archive.read_whatsapp_last_run() is not None
    assert client.get("/health").json()["last_poll"]["messages"] == {"portal": 1, "whatsapp": 1}
    service.portal_archive.write_json(service.daily_brief.OUTPUT_PATH, {
        "portal": {"messages": [], "error": None}, "whatsapp": {"messages": [], "error": None},
    })
    response = client.post("/brief")
    assert response.status_code == 200
    assert response.json()["homework"] == ["New homework"]
    assert response.json()["reminders"] == ["New notice"]


@pytest.mark.parametrize("workflow", ["initialize", "refresh", "cron", "scrape-udt"])
def test_attendance_is_collected_in_all_portal_workflows(client, monkeypatch, workflow):
    import json

    today = datetime.now(service.portal_archive.IST).date().isoformat()
    records = [{"date": today, "status": "present"}]
    calls = []
    monkeypatch.setattr(service.daily_brief, "fetch_attendance", lambda *args: calls.append(args) or records)
    monkeypatch.setattr(service.daily_brief, "fetch_recent_messages", lambda **kwargs: [])
    monkeypatch.setattr(service.daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])
    if workflow == "cron":
        client.app.state.scheduler.get_job("collect_messages").func()
        summary = client.get("/health").json()["last_poll"]
    else:
        response = client.post(f"/{workflow}")
        assert response.status_code == 200
        summary = response.json()
    assert len(calls) == 1
    assert summary["attendance_records"] == 1
    saved = json.loads(service.daily_brief.OUTPUT_PATH.read_text())
    assert saved["attendance"]["records"] == records
    if workflow in {"refresh", "cron"}:
        calendar = client.get("/brief").json()["calendar"]
        assert next(day for day in calendar if day["date"] == today)["attendance"] == "present"


@pytest.mark.parametrize("manual", [False, True])
def test_cron_and_refresh_collect_cache_generate_and_save(client, monkeypatch, manual, caplog):
    calls = []
    data = {"portal": {"messages": [], "error": None}}
    monkeypatch.setattr(service.daily_brief, "run", lambda: calls.append("collect") or data)
    monkeypatch.setattr(service, "prepare_storage", lambda data: calls.append("cache") or {
        "messages": {"portal": 0}, "warnings": ["Collection warning"],
    })
    def generate(collected, now, **kwargs):
        assert collected == data
        calls.append("generate")
        content = service.generate_brief.empty_brief("date", ["Collection warning", "Generation warning"])
        content.homework = ["Generated homework"]
        return content

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    if manual:
        response = client.post("/refresh")
        assert response.status_code == 200
        assert response.json()["brief_generated"] is True
        assert response.json()["warnings"] == ["Collection warning", "Generation warning"]
        assert "Scheduled brief warning" not in caplog.text
    else:
        client.app.state.scheduler.get_job("collect_messages").func()
        assert "Scheduled brief warning: Collection warning" in caplog.text
        assert "Scheduled brief warning: Generation warning" in caplog.text
    assert calls == ["collect", "cache", "generate"]
    assert client.get("/brief").json()["homework"] == ["Generated homework"]
    assert "warnings" not in client.get("/brief").json()
    assert "warnings" not in client.post("/brief").json()
    assert client.get("/health").json()["last_poll"]["messages"] == {"portal": 0}
    assert "warnings" not in client.get("/health").json()["last_poll"]


def test_busy_operations_are_rejected_and_polls_skip(client, monkeypatch):
    lock = client.app.state.operation_lock
    lock.acquire()
    try:
        assert client.post("/refresh").status_code == 409
        assert client.post("/initialize").status_code == 409
        assert client.post("/scrape-udt").status_code == 409
        assert client.post("/fetch-whatsapp").status_code == 409
        monkeypatch.setattr(service.daily_brief, "run", lambda: pytest.fail("Busy poll must skip"))
        client.app.state.scheduler.get_job("collect_messages").func()
    finally:
        lock.release()


def test_refresh_and_cron_reuse_saved_brief_when_unchanged(client, monkeypatch):
    data = {"portal": {"messages": [], "error": None}}
    monkeypatch.setattr(service.daily_brief, "run", lambda: data)
    response = client.post("/refresh")
    assert response.status_code == 200
    assert response.json()["brief_generated"] is True
    assert response.json()["brief_reused"] is False
    original = client.get("/brief").json()
    monkeypatch.setattr(service.generate_brief, "generate", lambda *args, **kwargs: pytest.fail("Unchanged run must reuse brief"))
    response = client.post("/refresh")
    assert response.status_code == 200
    assert response.json()["brief_generated"] is False
    assert response.json()["brief_reused"] is True
    client.app.state.scheduler.get_job("collect_messages").func()
    assert client.get("/health").json()["last_poll"]["brief_reused"] is True
    assert client.get("/brief").json() == original


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


def test_invalid_saved_brief_is_not_returned(client, monkeypatch):
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


@pytest.mark.parametrize("manual", [False, True])
def test_failed_refresh_preserves_previous_brief_and_reports_failure(client, monkeypatch, manual):
    content = service.generate_brief.empty_brief("date", [])
    content.homework = ["Previous homework"]
    service.generate_brief.write_content(content)
    monkeypatch.setattr(service.daily_brief, "run", lambda: {"portal": {"messages": []}})

    def fail(*args, **kwargs):
        raise RuntimeError("sensitive failure")

    monkeypatch.setattr(service.generate_brief, "generate", fail)
    if manual:
        response = client.post("/refresh")
        assert response.status_code == 502
        assert "sensitive" not in response.text
    else:
        client.app.state.scheduler.get_job("collect_messages").func()
    assert client.get("/brief").json() == content.model_dump(mode="json", exclude={"warnings"})
    assert client.get("/health").json()["last_poll"]["error"] == "RuntimeError"
    assert client.get("/health").json()["last_poll"]["brief_generated"] is False
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
        assert client.get("/brief").status_code == 401
        assert client.post("/refresh").status_code == 401
        assert client.post("/brief", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/initialize").status_code == 401
        assert client.post("/scrape-udt").status_code == 401
        assert client.post("/fetch-whatsapp").status_code == 401
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