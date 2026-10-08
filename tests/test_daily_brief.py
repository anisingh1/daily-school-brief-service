import json

import pytest

import daily_brief
import portal_archive


def test_initialization_scans_all_portal_messages_ignoring_cursor(monkeypatch):
    from datetime import datetime

    calls = {}
    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: pytest.fail("Cursor should not limit initialization"))

    def fetch(**kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(daily_brief, "fetch_recent_messages", fetch)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])
    result = daily_brief.gather(initialize=True)
    assert calls == {"cutoff": datetime.min, "download_attachments": True}
    assert result["portal"]["error"] is None


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "data" / "portal_messages.json")
    monkeypatch.setattr(portal_archive, "WHATSAPP_ARCHIVE_PATH", tmp_path / "data" / "whatsapp_messages.json")
    monkeypatch.setattr(portal_archive, "LAST_RUN_PATH", tmp_path / "data" / "last_run.json")
    monkeypatch.setattr(portal_archive, "OUTPUT_DIR", tmp_path / "output")


def test_gather_captures_portal_error(monkeypatch):
    def failing_fetch(**kwargs):
        raise RuntimeError("login failed")

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", failing_fetch)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    result = daily_brief.gather()

    assert result["portal"]["error"] == "RuntimeError: login failed"
    assert result["portal"]["messages"] == []
    assert result["whatsapp"]["error"] is None


def test_gather_reuses_stored_messages_and_cleans_up_when_fetches_fail(monkeypatch):
    from datetime import datetime, timedelta

    now = datetime.now(portal_archive.IST)
    recent_portal = {"id": "recent", "posted_at": now.isoformat(), "attachments": []}
    old_portal = {"id": "old", "posted_at": (now - timedelta(days=36)).isoformat(), "attachments": []}
    recent_whatsapp = {"timestamp": now.isoformat(), "text": "stored", "attachments": []}
    portal_archive.save_archive([recent_portal, old_portal])
    portal_archive.save_archive([recent_whatsapp], portal_archive.WHATSAPP_ARCHIVE_PATH)

    def failing_fetch(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(daily_brief, "fetch_recent_messages", failing_fetch)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", failing_fetch)
    result = daily_brief.gather()
    assert result["portal"]["messages"] == [recent_portal]
    assert result["whatsapp"]["messages"] == [recent_whatsapp]
    assert result["portal"]["error"] == "RuntimeError: offline"
    assert result["whatsapp"]["error"] == "RuntimeError: offline"
    assert portal_archive.load_archive() == [recent_portal]


def test_gather_stores_and_reuses_downloaded_sources(monkeypatch):
    from datetime import datetime

    now = datetime.now(portal_archive.IST).isoformat()
    portal = {"id": "1", "posted_at": now, "body": "Homework", "attachments": []}
    whatsapp = {"timestamp": now, "sender": "School", "text": "Notice", "attachments": []}
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [portal])
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [whatsapp])
    result = daily_brief.gather()
    assert result["portal"]["messages"] == portal_archive.load_archive() == [portal]
    assert result["whatsapp"]["messages"] == portal_archive.load_archive(portal_archive.WHATSAPP_ARCHIVE_PATH) == [whatsapp]
    assert daily_brief.gather() == result


def test_gather_merges_and_saves_archive_on_portal_success(monkeypatch):
    saved = {}
    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [{"id": "new", "title": "x"}])
    monkeypatch.setattr(
        daily_brief,
        "merge_into_archive",
        lambda new: [{"id": "new", "title": "x"}, {"id": "old", "title": "y"}],
    )
    monkeypatch.setattr(daily_brief, "prune_archive", lambda archive: archive)
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: saved.setdefault("archive", archive))
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: saved.setdefault("last_run", dt))
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    result = daily_brief.gather()

    assert result["portal"]["messages"] == [{"id": "new", "title": "x"}, {"id": "old", "title": "y"}]
    assert saved["archive"] == [{"id": "new", "title": "x"}, {"id": "old", "title": "y"}]
    assert "last_run" in saved


def test_gather_does_not_save_archive_or_cursor_on_portal_failure(monkeypatch):
    saved = {}

    def failing_fetch(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", failing_fetch)
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: saved.setdefault("archive", archive))
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: saved.setdefault("last_run", dt))
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    daily_brief.gather()

    assert "archive" not in saved
    assert "last_run" not in saved


def test_gather_does_not_advance_cursor_if_archive_save_fails(monkeypatch):
    saved = {}

    def failing_save_archive(archive):
        raise RuntimeError("disk full")

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [{"id": "new", "title": "x"}])
    monkeypatch.setattr(daily_brief, "merge_into_archive", lambda new: [{"id": "new", "title": "x"}])
    monkeypatch.setattr(daily_brief, "prune_archive", lambda archive: archive)
    monkeypatch.setattr(daily_brief, "save_archive", failing_save_archive)
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: saved.setdefault("last_run", dt))
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    result = daily_brief.gather()

    assert "last_run" not in saved
    assert result["portal"]["error"] == "RuntimeError: disk full"


def test_gather_captures_whatsapp_error(monkeypatch):
    def failing_fetch(**kwargs):
        raise RuntimeError("drive unreachable")

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [])
    monkeypatch.setattr(daily_brief, "merge_into_archive", lambda new: [])
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: None)
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: None)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", failing_fetch)

    result = daily_brief.gather()

    assert result["whatsapp"]["error"] == "RuntimeError: drive unreachable"
    assert result["portal"]["error"] is None


def test_gather_uses_35_day_whatsapp_cutoff(monkeypatch):
    from datetime import datetime, timedelta

    calls = {}

    def fake_whatsapp_fetch(**kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [])
    monkeypatch.setattr(daily_brief, "merge_into_archive", lambda new: [])
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: None)
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: None)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", fake_whatsapp_fetch)

    daily_brief.gather()

    assert abs((datetime.now(portal_archive.IST) - calls["cutoff"]) - timedelta(days=35)) < timedelta(seconds=2)


def test_gather_uses_portal_cutoff_and_downloads_attachments(monkeypatch):
    calls = {}

    def fake_fetch(**kwargs):
        calls.update(kwargs)
        return []

    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", fake_fetch)
    monkeypatch.setattr(daily_brief, "merge_into_archive", lambda new: [])
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: None)
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: None)
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    daily_brief.gather()

    assert calls["cutoff"] == "fixed-cutoff"
    assert calls["download_attachments"] is True


def test_gather_prunes_archive_before_saving(monkeypatch):
    saved = {}
    monkeypatch.setattr(daily_brief, "compute_portal_cutoff", lambda: "fixed-cutoff")
    monkeypatch.setattr(daily_brief, "fetch_recent_messages", lambda **kwargs: [{"id": "new", "title": "x"}])
    monkeypatch.setattr(
        daily_brief,
        "merge_into_archive",
        lambda new: [{"id": "new", "title": "x"}, {"id": "old", "title": "y"}],
    )
    monkeypatch.setattr(
        daily_brief,
        "prune_archive",
        lambda archive: [m for m in archive if m["id"] != "old"],
    )
    monkeypatch.setattr(daily_brief, "save_archive", lambda archive: saved.setdefault("archive", archive))
    monkeypatch.setattr(daily_brief, "save_last_run", lambda dt: saved.setdefault("last_run", dt))
    monkeypatch.setattr(daily_brief, "fetch_recent_whatsapp_messages", lambda **kwargs: [])

    result = daily_brief.gather()

    assert result["portal"]["messages"] == [{"id": "new", "title": "x"}]
    assert saved["archive"] == [{"id": "new", "title": "x"}]


def test_run_writes_output_file(monkeypatch, tmp_path):
    fixed_result = {
        "portal": {"messages": [], "error": None},
        "whatsapp": {"messages": [], "error": None},
    }
    monkeypatch.setattr(daily_brief, "gather", lambda: fixed_result)
    out_path = tmp_path / "out.json"
    monkeypatch.setattr(daily_brief, "OUTPUT_PATH", out_path)

    result = daily_brief.run()

    assert out_path.exists()
    assert json.loads(out_path.read_text()) == result == fixed_result


@pytest.mark.parametrize("from_input", [False, True])
def test_pipeline_generates_writes_and_previews(monkeypatch, tmp_path, from_input):
    data = {"portal": {"messages": [{"body": "homework"}], "error": None}}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(data))
    monkeypatch.setattr(daily_brief, "OUTPUT_PATH", input_path)
    monkeypatch.setattr(daily_brief.generate_brief, "CONTENT_PATH", tmp_path / "content.json")
    calls = []
    def collect():
        calls.append("collect")
        return data
    def generate(source_data, now):
        assert source_data == data
        assert now.tzinfo == daily_brief.generate_brief.IST
        calls.append("generate")
        return daily_brief.generate_brief.empty_brief("date", [])
    monkeypatch.setattr(daily_brief, "run", collect)
    monkeypatch.setattr(daily_brief.generate_brief, "generate", generate)
    monkeypatch.setattr(daily_brief.send_email, "run", lambda preview: calls.append(("send", preview)))
    monkeypatch.setattr(daily_brief.commit_archive, "commit_and_push", lambda: pytest.fail("Unexpected commit"))
    assert daily_brief.run_pipeline(preview=True, from_input=from_input)
    assert calls == (["generate", ("send", True)] if from_input else ["collect", "generate", ("send", True)])
    assert json.loads((tmp_path / "content.json").read_text())["homework"] == []


@pytest.mark.parametrize("stage", ["collect", "generate", "archive"])
def test_pipeline_replaces_stale_content_with_warning_and_still_delivers(monkeypatch, tmp_path, stage):
    content_path = tmp_path / "content.json"
    content_path.write_text('{"homework": ["stale"]}')
    monkeypatch.setattr(daily_brief.generate_brief, "CONTENT_PATH", content_path)
    data = {"portal": {"messages": [], "error": "portal unavailable"}}
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive detail that should not be emailed")
    monkeypatch.setattr(daily_brief, "run", fail if stage == "collect" else lambda: data)
    monkeypatch.setattr(daily_brief.generate_brief, "generate", fail if stage == "generate" else lambda *args, **kwargs: daily_brief.generate_brief.empty_brief("date", []))
    monkeypatch.setattr(daily_brief.commit_archive, "commit_and_push", fail if stage == "archive" else lambda: pytest.fail("Unexpected commit"))
    sent = []
    monkeypatch.setattr(daily_brief.send_email, "run", lambda preview: sent.append(preview))
    assert not daily_brief.run_pipeline(persist_archive=stage == "archive")
    result = json.loads(content_path.read_text())
    assert result["homework"] == []
    assert "RuntimeError" in result["warnings"][-1]
    assert "sensitive detail" not in content_path.read_text()
    assert sent == [False]


def test_pipeline_includes_recoverable_archive_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_brief.generate_brief, "CONTENT_PATH", tmp_path / "content.json")
    monkeypatch.setattr(daily_brief, "run", lambda: {})
    monkeypatch.setattr(daily_brief.commit_archive, "commit_and_push", lambda: print("WARNING: push rejected"))
    monkeypatch.setattr(daily_brief.generate_brief, "generate", lambda *args, **kwargs: daily_brief.generate_brief.empty_brief("date", []))
    monkeypatch.setattr(daily_brief.send_email, "run", lambda preview: None)
    assert daily_brief.run_pipeline(preview=True, persist_archive=True)
    result = json.loads((tmp_path / "content.json").read_text())
    assert "could not be pushed" in result["warnings"][0]


@pytest.mark.parametrize("generation_fails", [False, True])
def test_archive_commit_runs_after_generation_even_on_failure(monkeypatch, tmp_path, generation_fails):
    monkeypatch.setattr(daily_brief.generate_brief, "CONTENT_PATH", tmp_path / "content.json")
    monkeypatch.setattr(daily_brief, "run", lambda: {})
    calls = []

    def generate(*args, **kwargs):
        calls.append("generate")
        if generation_fails:
            raise RuntimeError("failed")
        return daily_brief.generate_brief.empty_brief("date", [])

    monkeypatch.setattr(daily_brief.generate_brief, "generate", generate)
    monkeypatch.setattr(daily_brief.commit_archive, "commit_and_push", lambda: calls.append("commit"))
    monkeypatch.setattr(daily_brief.send_email, "run", lambda preview: calls.append("send"))
    assert daily_brief.run_pipeline(persist_archive=True) is not generation_fails
    assert calls == ["generate", "commit", "send"]
