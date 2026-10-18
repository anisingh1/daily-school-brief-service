import base64
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import service

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5S8AAAAASUVORK5CYII="
)


@pytest.fixture
def connections(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_API_TOKEN", "test-token")
    monkeypatch.setenv("MESSAGE_POLL_CRON", "0 0 1 1 *")
    monkeypatch.setenv("EVOLUTION_API_URL", "http://evolution.test")
    monkeypatch.setenv("EVOLUTION_API_KEY", "test-evolution-key")
    monkeypatch.setenv("EVOLUTION_INSTANCE", "school")
    state = {
        "connection": "open", "pairing": {"instance": {"state": "open"}},
        "requests": [], "portal_calls": 0, "portal_error": None,
        "status": 200, "timeout": False,
    }

    def portal_check():
        state["portal_calls"] += 1
        if state["portal_error"] is not None:
            raise state["portal_error"]

    def handle(request):
        state["requests"].append(request)
        assert request.headers["apikey"] == "test-evolution-key"
        if state["timeout"]:
            raise httpx.ReadTimeout("private connection details")
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"secret": "upstream credentials"})
        if "/connectionState/" in request.url.path:
            return httpx.Response(200, json={"instance": {"state": state["connection"]}})
        assert request.method == "GET" and request.url.path == "/instance/connect/school"
        return httpx.Response(200, json=state["pairing"])

    monkeypatch.setattr(service.portal, "check_connection", portal_check)
    monkeypatch.setattr(service.fetch_evolution, "create_client", lambda settings: httpx.Client(
        base_url=settings["EVOLUTION_API_URL"], headers={"apikey": settings["EVOLUTION_API_KEY"]},
        transport=httpx.MockTransport(handle),
    ))
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: pytest.fail("Unexpected message collection"))
    monkeypatch.setattr(service.generate_brief, "create_client", lambda: pytest.fail("Unexpected Azure call"))
    monkeypatch.setattr(service.generate_brief, "CONTENT_PATH", tmp_path / "brief.json")
    with TestClient(service.create_app(), headers={"Authorization": "Bearer test-token"}) as client:
        yield client, state, tmp_path


def test_default_health_never_probes_external_services(connections):
    client, state, _ = connections
    for _ in range(3):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["connections"] == {
            name: {"connected": None, "state": "unknown", "checked_at": None, "error": None, "stale": True}
            for name in ("udt", "whatsapp")
        }
    assert state["requests"] == []
    assert state["portal_calls"] == 0


def test_live_health_checks_both_sources_and_caches_results(connections):
    client, state, _ = connections
    response = client.get("/health?check_connections=true")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    for name, expected in (("udt", "authenticated"), ("whatsapp", "open")):
        status = result["connections"][name]
        assert status["connected"] is True
        assert status["state"] == expected
        assert datetime.fromisoformat(status["checked_at"]).tzinfo is not None
        assert status["error"] is None
        assert status["stale"] is False
    assert client.get("/health").json()["connections"] == result["connections"]
    assert state["portal_calls"] == 1
    assert len(state["requests"]) == 1
    assert state["requests"][0].extensions["timeout"]["read"] == 10


def test_cached_connection_results_explicitly_become_stale(connections, monkeypatch):
    client, state, _ = connections
    original = client.get("/health?check_connections=true").json()["connections"]
    later = datetime.fromisoformat(original["whatsapp"]["checked_at"]) + timedelta(minutes=6)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(service, "datetime", Later)
    cached = client.get("/health").json()["connections"]
    assert all(status["stale"] for status in cached.values())
    assert cached["whatsapp"]["checked_at"] == original["whatsapp"]["checked_at"]
    assert state["portal_calls"] == len(state["requests"]) == 1


@pytest.mark.parametrize("connection", ["close", "connecting", None])
def test_health_reports_disconnected_or_initializing_whatsapp_without_pairing(connections, connection):
    client, state, _ = connections
    state["connection"] = connection
    result = client.get("/health?check_connections=true").json()
    assert result["connections"]["whatsapp"]["connected"] is False
    assert result["connections"]["whatsapp"]["state"] == (connection or "initializing")
    assert result["connections"]["udt"]["connected"] is True
    assert len(state["requests"]) == 1
    assert "/connectionState/" in state["requests"][0].url.path


@pytest.mark.parametrize("failure", ["portal", "upstream-status", "timeout", "invalid-state"])
def test_dependency_failure_is_visible_without_failing_app_liveness(connections, failure, caplog):
    client, state, _ = connections
    if failure == "portal":
        state["portal_error"] = RuntimeError("sensitive portal credentials")
    elif failure == "upstream-status":
        state["status"] = 401
    elif failure == "timeout":
        state["timeout"] = True
    else:
        state["connection"] = {"invalid": True}
    response = client.get("/health?check_connections=true")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    name = "udt" if failure == "portal" else "whatsapp"
    status = response.json()["connections"][name]
    assert status["connected"] is False
    assert status["state"] == "error"
    assert status["error"]
    assert "credentials" not in response.text + caplog.text
    assert "private connection details" not in response.text + caplog.text
    assert not client.app.state.operation_lock.locked()


def test_qr_returns_png_without_writing_credentials_to_disk(connections):
    client, state, tmp_path = connections
    state["pairing"] = {"base64": "data:image/png;base64," + base64.b64encode(PNG).decode()}
    response = client.post("/whatsapp/qr")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == PNG
    assert list(tmp_path.iterdir()) == []
    assert client.get("/health").json()["connections"]["whatsapp"]["state"] == "qr_required"
    assert not client.app.state.operation_lock.locked()


def test_already_connected_qr_request_does_not_log_out_or_recreate_instance(connections):
    client, state, _ = connections
    response = client.post("/whatsapp/qr")
    assert response.status_code == 409
    assert "already connected" in response.json()["detail"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/health").json()["connections"]["whatsapp"]["connected"] is True
    assert len(state["requests"]) == 1
    assert state["requests"][0].url.path == "/instance/connect/school"
    assert not client.app.state.operation_lock.locked()


@pytest.mark.parametrize("result", [{}, {"count": 0}, {"base64": ""}, {"base64": None}])
def test_pending_qr_returns_retry_response_instead_of_fake_image(connections, result):
    client, state, _ = connections
    state["pairing"] = result
    response = client.post("/whatsapp/qr")
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/health").json()["connections"]["whatsapp"]["state"] == "connecting"
    assert not client.app.state.operation_lock.locked()


@pytest.mark.parametrize("result", [
    [], {"instance": []}, {"error": True, "message": "private upstream credentials"},
    {"base64": 123}, {"base64": "bad base64"}, {"base64": base64.b64encode(b"not PNG").decode()},
    {"base64": "A" * (3 * 1024 * 1024 + 1)},
])
def test_invalid_upstream_qr_is_rejected(connections, result, caplog):
    client, state, _ = connections
    state["pairing"] = result
    response = client.post("/whatsapp/qr")
    assert response.status_code == 502
    assert response.headers["cache-control"] == "no-store"
    assert "credentials" not in response.text + caplog.text
    assert not client.app.state.operation_lock.locked()


def test_qr_timeout_reports_failure_and_releases_lock(connections):
    client, state, _ = connections
    state["timeout"] = True
    response = client.post("/whatsapp/qr")
    assert response.status_code == 502
    assert "private connection details" not in response.text
    assert not client.app.state.operation_lock.locked()


def test_qr_and_live_probes_require_authorization(connections):
    client, state, _ = connections
    client.headers.pop("Authorization")
    for route in ("/whatsapp/qr", "/health?check_connections=true"):
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            response = client.post(route, headers=headers) if route.startswith("/whatsapp") else client.get(route, headers=headers)
            assert response.status_code == 401
    assert state["requests"] == []
    assert state["portal_calls"] == 0


def test_pairing_requires_a_configured_token_even_on_localhost(connections, monkeypatch):
    _, state, _ = connections
    monkeypatch.delenv("SERVICE_API_TOKEN")
    with TestClient(service.create_app()) as client:
        response = client.post("/whatsapp/qr")
        assert response.status_code == 503
        assert "Configure SERVICE_API_TOKEN" in response.json()["detail"]
    assert state["requests"] == []


def test_busy_pairing_and_live_checks_do_not_disrupt_collection(connections):
    client, state, _ = connections
    with client.app.state.operation_lock:
        assert client.post("/whatsapp/qr").status_code == 409
        assert client.get("/health?check_connections=true").status_code == 409
        assert client.get("/health").json()["busy"] is True
    assert state["requests"] == []
    assert state["portal_calls"] == 0


@pytest.mark.parametrize("error", [None, "RuntimeError: sensitive collection failure"])
def test_source_collection_updates_cached_status_without_claiming_other_source_was_checked(connections, monkeypatch, error):
    client, state, _ = connections
    monkeypatch.setattr(service.daily_brief, "run", lambda **kwargs: {
        "portal": {"messages": [], "error": error},
        "whatsapp": {"messages": [], "error": None},
    })
    response = client.post("/scrape-udt")
    assert response.status_code == (502 if error else 200)
    status = client.get("/health").json()["connections"]
    assert status["udt"]["connected"] is (None if error else True)
    assert status["udt"]["state"] == ("collection_failed" if error else "authenticated")
    assert "sensitive" not in str(status)
    assert status["whatsapp"]["checked_at"] is None
    assert state["portal_calls"] == len(state["requests"]) == 0
