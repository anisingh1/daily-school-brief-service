from datetime import datetime
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
    cutoff = datetime(2026, 10, 8, 10)
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
    previous = datetime(2026, 10, 8, 10)
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
    previous = datetime(2026, 10, 8, 10)
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

    def generate(data, now):
        calls.append(data)
        content = service.generate_brief.empty_brief("date", [])
        content.homework = ["Read page 12"]
        return content

    monkeypatch.setattr(service.generate_brief, "generate", generate)
    monkeypatch.setattr(service.daily_brief.send_email, "run", lambda **kwargs: pytest.fail("Unexpected email"))
    response = client.post("/brief")
    assert response.status_code == 200
    assert response.json()["homework"] == ["Read page 12"]
    assert calls[0]["portal"]["messages"] == [message]
    assert service.generate_brief.CONTENT_PATH.exists()


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

    def generate(data, now):
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
    monkeypatch.setattr(service.daily_brief, "run", lambda: {})
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


def test_public_binding_requires_authentication(monkeypatch):
    monkeypatch.setenv("SERVICE_HOST", "0.0.0.0")
    monkeypatch.delenv("SERVICE_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="SERVICE_API_TOKEN"):
        service.run()