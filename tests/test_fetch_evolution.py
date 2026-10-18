import base64
import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import evolution_admin
import fetch_evolution
import fetch_whatsapp

GROUP = "120363000001@g.us"
PDF = b"%PDF-1.4 test document"


@pytest.fixture
def evolution(monkeypatch, tmp_path):
    for name, value in {
        "WHATSAPP_SOURCE": "evolution",
        "EVOLUTION_API_URL": "http://evolution.test",
        "EVOLUTION_API_KEY": "test-key",
        "EVOLUTION_INSTANCE": "school",
        "EVOLUTION_GROUP_JID": GROUP,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(fetch_whatsapp, "ATTACHMENTS_DIR", tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    record = {
        "key": {"id": "notice-1", "remoteJid": GROUP, "participant": "teacher@s.whatsapp.net"},
        "pushName": "Teacher",
        "messageTimestamp": int((now - timedelta(minutes=5)).timestamp()),
        "message": {"conversation": "Bring the workbook"},
    }
    state = {
        "record": record, "pages": [[record]], "connection": "open",
        "media": base64.b64encode(PDF).decode(), "requests": [], "status": 200,
    }
    real_client = httpx.Client

    def handle(request):
        state["requests"].append(request)
        assert request.headers["apikey"] == "test-key"
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": "test failure"})
        if "/connectionState/" in request.url.path:
            return httpx.Response(200, json={"instance": {"state": state["connection"]}})
        if "/getBase64FromMediaMessage/" in request.url.path:
            if state.get("media_timeout"):
                raise httpx.ReadTimeout("Simulated media timeout")
            return httpx.Response(state.get("media_status", 200), json={"base64": state["media"]})
        if "/instance/connect/" in request.url.path:
            return httpx.Response(200, json=state["pairing"])
        if "/group/fetchAllGroups/" in request.url.path:
            return httpx.Response(200, json=state.get("groups", [{"id": GROUP, "subject": "School"}]))
        if request.url.path == "/instance/create":
            state["create_body"] = json.loads(request.content)
            return httpx.Response(201, json={"instance": {"instanceName": "school"}})
        body = json.loads(request.content)
        assert body["where"]["key"] == {"remoteJid": GROUP, "remoteJidAlt": GROUP}
        assert body["offset"] == fetch_evolution.PAGE_SIZE
        assert set(body["where"]["messageTimestamp"]) == {"gte", "lte"}
        page = body["page"]
        if "response" in state:
            return httpx.Response(200, json=state["response"])
        return httpx.Response(200, json={"messages": {
            "records": state["pages"][page - 1], "pages": len(state["pages"]),
            "currentPage": page, "total": sum(map(len, state["pages"])),
        }})

    monkeypatch.setattr(fetch_evolution.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handle),
    ))
    return state, now - timedelta(days=1), tmp_path


def collect(evolution):
    _, cutoff, _ = evolution
    return fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=cutoff)


def test_dispatch_pagination_group_filtering_and_stable_ids(evolution):
    state, cutoff, _ = evolution
    other = copy.deepcopy(state["record"])
    other["key"] = {"id": "private", "remoteJid": "private@s.whatsapp.net"}
    old = copy.deepcopy(state["record"])
    old["messageTimestamp"] = int((cutoff - timedelta(seconds=1)).timestamp())
    future = copy.deepcopy(state["record"])
    future["messageTimestamp"] = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    second = copy.deepcopy(state["record"])
    second["key"]["id"] = "notice-2"
    second["message"] = {"extendedTextMessage": {"text": "Tomorrow: sports uniform"}}
    second["messageTimestamp"] += 60
    state["pages"] = [[second, other, future], [state["record"], old, second]]
    first = collect(evolution)
    assert [message["text"] for message in first] == ["Bring the workbook", "Tomorrow: sports uniform"]
    assert first[0]["sender"] == "Teacher"
    assert first[0]["attachments"] == []
    assert first[0]["timestamp"].endswith("+00:00")
    assert first[0]["id"].startswith("evolution:")
    assert collect(evolution) == first


@pytest.mark.parametrize("wrapper", [None, "ephemeralMessage", "documentWithCaptionMessage"])
def test_pdf_caption_download_safe_filename_and_reuse(evolution, wrapper):
    state, _, directory = evolution
    content = {"documentMessage": {
        "fileName": "../../escape.pdf", "mimetype": "application/pdf", "caption": "Homework attached",
    }}
    state["record"]["message"] = {wrapper: {"message": content}} if wrapper else content
    result = collect(evolution)
    attachment = result[0]["attachments"][0]
    assert result[0]["text"] == "Homework attached"
    assert attachment["name"] == "escape.pdf"
    files = list(directory.iterdir())
    assert len(files) == 1
    assert str(files[0]) == attachment["saved_as"]
    assert files[0].read_bytes() == PDF
    assert files[0].name.startswith("whatsapp_evolution_")
    media_request = next(r for r in state["requests"] if "/getBase64" in r.url.path)
    assert json.loads(media_request.content) == {
        "message": {"key": state["record"]["key"]}, "convertToMp4": False,
    }
    collect(evolution)
    assert sum("/getBase64" in request.url.path for request in state["requests"]) == 1


@pytest.mark.parametrize("content,expected", [
    ({"imageMessage": {"caption": "Bring this tomorrow"}}, "Bring this tomorrow"),
    ({"videoMessage": {"caption": "Sports day"}}, "Sports day"),
    ({"documentMessage": {"fileName": "notice.docx", "caption": "Read the notice"}}, "Read the notice"),
    ({"audioMessage": {"url": "not-downloaded"}}, None),
    ({"protocolMessage": {"type": 0}}, None),
])
def test_supported_captions_and_unsupported_media(evolution, content, expected):
    state, _, directory = evolution
    state["record"]["message"] = content
    result = collect(evolution)
    assert [message["text"] for message in result] == ([] if expected is None else [expected])
    assert list(directory.iterdir()) == []


def test_remote_jid_alt_and_sender_fallback(evolution):
    state, _, _ = evolution
    state["record"]["key"]["remoteJidAlt"] = GROUP
    state["record"]["key"]["remoteJid"] = "alternate"
    state["record"]["pushName"] = None
    assert collect(evolution)[0]["sender"] == "teacher@s.whatsapp.net"


@pytest.mark.parametrize("connection", ["close", "connecting", None])
def test_disconnected_instance_fails_without_claiming_fresh_data(evolution, connection):
    state, _, _ = evolution
    state["connection"] = connection
    with pytest.raises(RuntimeError, match="disconnected"):
        collect(evolution)
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("status", [401, 404, 429, 500])
def test_http_failure_is_propagated(evolution, status):
    state, _, _ = evolution
    state["status"] = status
    with pytest.raises(httpx.HTTPStatusError):
        collect(evolution)


@pytest.mark.parametrize("response", [
    {}, {"messages": []},
    {"messages": {"pages": 1, "records": [], "currentPage": 1}},
    {"messages": {"pages": "1", "records": [], "currentPage": 1}},
    {"messages": {"pages": 0, "records": [], "currentPage": 2}},
    {"messages": {"pages": 0, "records": [{}], "currentPage": 1}},
])
def test_invalid_api_response_is_not_an_empty_success(evolution, response):
    state, _, _ = evolution
    state["response"] = response
    with pytest.raises((TypeError, ValueError)):
        collect(evolution)


def test_empty_group_is_valid(evolution):
    state, _, _ = evolution
    state["response"] = {"messages": {"pages": 0, "records": [], "currentPage": 1}}
    assert collect(evolution) == []


@pytest.mark.parametrize("field,value", [
    ("messageTimestamp", None), ("messageTimestamp", True), ("messageTimestamp", "bad"),
    ("message", None), ("message", {"conversation": 123}), ("key", {}),
])
def test_malformed_school_messages_fail_explicitly(evolution, field, value):
    state, _, _ = evolution
    if field == "key":
        state["record"]["key"].pop("id")
    else:
        state["record"][field] = value
    with pytest.raises((TypeError, ValueError)):
        collect(evolution)


@pytest.mark.parametrize("media", ["", None, "%%invalid%%", base64.b64encode(b"not a PDF").decode()])
def test_invalid_pdf_does_not_leave_a_file(evolution, media):
    state, _, directory = evolution
    state["record"]["message"] = {"documentMessage": {"fileName": "notice.pdf"}}
    state["media"] = media
    with pytest.raises(ValueError):
        collect(evolution)
    assert list(directory.iterdir()) == []


def test_pdf_size_limit_and_data_uri(evolution, monkeypatch):
    state, _, directory = evolution
    state["record"]["message"] = {"documentMessage": {"mimetype": "application/pdf"}}
    state["media"] = "data:application/pdf;base64," + state["media"]
    monkeypatch.setattr(fetch_evolution, "MAX_PDF_BYTES", len(PDF) - 1)
    with pytest.raises(ValueError, match="limit|oversized"):
        collect(evolution)
    assert list(directory.iterdir()) == []
    monkeypatch.setattr(fetch_evolution, "MAX_PDF_BYTES", len(PDF))
    assert collect(evolution)[0]["attachments"][0]["name"] == "document.pdf"


@pytest.mark.parametrize("name,value", [
    ("EVOLUTION_API_URL", ""), ("EVOLUTION_API_KEY", ""), ("EVOLUTION_INSTANCE", ""),
    ("EVOLUTION_GROUP_JID", ""), ("EVOLUTION_GROUP_JID", "School group"),
    ("EVOLUTION_API_URL", "file:///tmp/api"),
    ("EVOLUTION_API_URL", "http://user:password@example.test"),
    ("EVOLUTION_API_URL", "https://example.test?apikey=secret"),
    ("WHATSAPP_SOURCE", "typo"),
    ("WHATSAPP_SOURCE", "email"),
])
def test_invalid_configuration_fails_before_network(evolution, monkeypatch, name, value):
    state, _, _ = evolution
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        collect(evolution)
    assert state["requests"] == []


def test_evolution_is_the_default_source(monkeypatch):
    monkeypatch.delenv("WHATSAPP_SOURCE", raising=False)
    cutoff = datetime.now(timezone.utc)
    calls = []
    monkeypatch.setattr(fetch_whatsapp, "fetch_recent_evolution_messages", lambda *args: calls.append(args) or [])
    assert fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=cutoff) == []
    assert calls == [(cutoff, fetch_whatsapp.ATTACHMENTS_DIR)]


def test_admin_creates_read_only_instance(evolution):
    state, _, _ = evolution
    evolution_admin.run("create")
    assert state["create_body"] == {
        "instanceName": "school", "integration": "WHATSAPP-BAILEYS", "qrcode": False,
        "groupsIgnore": False, "syncFullHistory": True,
        "alwaysOnline": False, "readMessages": False, "readStatus": False,
    }


def test_admin_pairing_saves_private_qr_and_status_removes_it(evolution, monkeypatch):
    state, _, directory = evolution
    image = b"\x89PNG\r\n\x1a\nfake-qr-image"
    state["pairing"] = {"base64": "data:image/png;base64," + base64.b64encode(image).decode()}
    qr_path = directory / "qr.png"
    monkeypatch.setattr(evolution_admin, "QR_PATH", qr_path)
    evolution_admin.run("pair")
    assert qr_path.read_bytes() == image
    assert qr_path.stat().st_mode & 0o777 == 0o600
    evolution_admin.run("status")
    assert not qr_path.exists()


def test_admin_rejects_missing_pairing_image(evolution, monkeypatch):
    state, _, directory = evolution
    state["pairing"] = {"count": 0}
    monkeypatch.setattr(evolution_admin, "QR_PATH", directory / "qr.png")
    with pytest.raises(ValueError, match="No QR"):
        evolution_admin.run("pair")


def test_admin_selects_only_a_known_group_and_preserves_other_settings(evolution, monkeypatch):
    _, _, directory = evolution
    config = directory / ".env"
    config.write_text("UNRELATED_SETTING=preserved\nWHATSAPP_SOURCE=email\n")
    monkeypatch.setattr(evolution_admin, "ENV_PATH", config)
    monkeypatch.setattr(evolution_admin, "QR_PATH", directory / "qr.png")
    for invalid in [None, "private@s.whatsapp.net", "different@g.us"]:
        with pytest.raises(ValueError, match="exact group ID"):
            evolution_admin.run("select-group", invalid)
        assert config.read_text() == "UNRELATED_SETTING=preserved\nWHATSAPP_SOURCE=email\n"
    evolution_admin.run("select-group", GROUP)
    from dotenv import dotenv_values

    assert dotenv_values(config) == {
        "UNRELATED_SETTING": "preserved", "WHATSAPP_SOURCE": "evolution", "EVOLUTION_GROUP_JID": GROUP,
    }


def test_failed_pdf_save_leaves_no_partial_file_and_can_retry(evolution, monkeypatch):
    state, _, directory = evolution
    state["record"]["message"] = {"documentMessage": {"fileName": "notice.pdf"}}
    original_replace = fetch_evolution.os.replace

    def fail(*args):
        raise OSError("disk error")

    monkeypatch.setattr(fetch_evolution.os, "replace", fail)
    with pytest.raises(OSError, match="disk error"):
        collect(evolution)
    assert list(directory.iterdir()) == []
    monkeypatch.setattr(fetch_evolution.os, "replace", original_replace)
    assert len(collect(evolution)[0]["attachments"]) == 1


@pytest.mark.parametrize("failure", ["http", "timeout"])
def test_unavailable_pdf_preserves_messages_warns_and_recovers_on_explicit_refetch(evolution, failure, caplog):
    import generate_brief
    import service

    state, _, directory = evolution
    state["record"]["message"] = {"documentMessage": {"fileName": "notice.pdf", "caption": "Read this"}}
    if failure == "http":
        state["media_status"] = 400
    else:
        state["media_timeout"] = True
    messages = collect(evolution)
    attachment = messages[0]["attachments"][0]
    assert messages[0]["text"] == "Read this"
    assert attachment["name"] == "notice.pdf"
    assert "saved_as" not in attachment
    assert "explicit initialization" in attachment["download_error"]
    assert "PDF download failed" in caplog.text
    assert list(directory.iterdir()) == []
    data = {"whatsapp": {"messages": messages, "error": None}}
    payload, warnings, images = generate_brief.prepare_input(data)
    assert len(warnings) == 1
    assert attachment["download_error"] in warnings[0]
    assert payload["source_warnings"] == warnings
    assert images == []
    assert service.prepare_storage(data)["warnings"] == warnings
    state["media_status"] = 200
    state["media_timeout"] = False
    recovered = collect(evolution)
    assert recovered[0]["id"] == messages[0]["id"]
    assert "download_error" not in recovered[0]["attachments"][0]
    assert generate_brief.attachment_warnings({"whatsapp": {"messages": recovered}}) == []


def test_incremental_collection_never_retries_old_pdfs(evolution, monkeypatch):
    import daily_brief
    import portal_archive

    state, _, directory = evolution
    monkeypatch.setattr(portal_archive, "DATA_DIR", directory / "data")
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", directory / "data" / "portal.json")
    monkeypatch.setattr(portal_archive, "WHATSAPP_ARCHIVE_PATH", directory / "data" / "whatsapp.json")
    monkeypatch.setattr(portal_archive, "LAST_RUN_PATH", directory / "data" / "last_run.json")
    monkeypatch.setattr(portal_archive, "OUTPUT_DIR", directory / "output")
    now = datetime.now(timezone.utc)
    previous = now - timedelta(minutes=3)
    portal_archive.save_whatsapp_last_run(previous)
    old = state["record"]
    old["message"] = {"documentMessage": {"fileName": "expired.pdf"}}
    retained = {
        "id": "evolution:old", "timestamp": datetime.fromtimestamp(old["messageTimestamp"], timezone.utc).isoformat(),
        "sender": "Teacher", "text": "", "attachments": [{
            "name": "expired.pdf", "download_error": "Previously unavailable",
        }],
    }
    portal_archive.save_archive([retained], portal_archive.WHATSAPP_ARCHIVE_PATH)
    new = copy.deepcopy(old)
    new["key"]["id"] = "new-pdf"
    new["messageTimestamp"] = int((now - timedelta(minutes=1)).timestamp())
    new["message"]["documentMessage"]["fileName"] = "new.pdf"
    state["pages"] = [[new, old]]
    for _ in range(2):
        cursor = portal_archive.read_whatsapp_last_run()
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=portal_archive.IST)
        result = daily_brief.gather(source="whatsapp")["whatsapp"]
        assert result["error"] is None
        assert len(result["messages"]) == 2
        query = next(
            json.loads(request.content) for request in reversed(state["requests"])
            if "/findMessages/" in request.url.path
        )
        requested_cutoff = datetime.fromisoformat(query["where"]["messageTimestamp"]["gte"])
        assert requested_cutoff > cursor
        assert requested_cutoff.microsecond == 0
    media_requests = [
        json.loads(request.content) for request in state["requests"] if "/getBase64" in request.url.path
    ]
    assert [request["message"]["key"]["id"] for request in media_requests] == ["new-pdf"]
    assert retained in portal_archive.load_archive(portal_archive.WHATSAPP_ARCHIVE_PATH)
