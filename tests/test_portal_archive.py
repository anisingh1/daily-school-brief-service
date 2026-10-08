import json
import os
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta

import pytest

import portal_archive


def test_retention_days_defaults_to_35(monkeypatch):
    monkeypatch.delenv("ARCHIVE_RETENTION_DAYS", raising=False)
    assert portal_archive._read_retention_days() == 35


def test_retention_days_reads_environment(monkeypatch):
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", "14")
    monkeypatch.setattr(portal_archive, "RETENTION_DAYS", portal_archive._read_retention_days())
    now = datetime(2026, 10, 7, 12)
    assert portal_archive.retention_cutoff(now) == now - timedelta(days=14)
    assert portal_archive.is_within_retention("2026-09-23T12:00:00", now)
    assert not portal_archive.is_within_retention("2026-09-23T11:59:59", now)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", ""])
def test_retention_days_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("ARCHIVE_RETENTION_DAYS", value)
    with pytest.raises(ValueError, match="ARCHIVE_RETENTION_DAYS must be a positive integer"):
        portal_archive._read_retention_days()


def test_read_last_run_returns_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "LAST_RUN_PATH", tmp_path / "last_run.json")
    assert portal_archive.read_last_run() is None


def test_save_and_read_last_run_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "DATA_DIR", tmp_path)
    monkeypatch.setattr(portal_archive, "LAST_RUN_PATH", tmp_path / "last_run.json")

    portal_archive.save_last_run(datetime(2026, 9, 5, 18, 0, 0))

    assert portal_archive.read_last_run() == datetime(2026, 9, 5, 18, 0, 0)


def test_compute_portal_cutoff_uses_last_run_when_present(monkeypatch):
    saved = datetime(2026, 9, 4, 19, 30)
    monkeypatch.setattr(portal_archive, "read_last_run", lambda: saved)
    assert portal_archive.compute_portal_cutoff() == saved


def test_compute_portal_cutoff_falls_back_to_retention_when_absent(monkeypatch):
    monkeypatch.setattr(portal_archive, "read_last_run", lambda: None)
    cutoff = datetime(2026, 9, 2)
    monkeypatch.setattr(portal_archive, "retention_cutoff", lambda: cutoff)
    assert portal_archive.compute_portal_cutoff() == cutoff


def test_retention_boundary_and_timezone():
    now = datetime(2026, 10, 7, 12)
    assert portal_archive.is_within_retention("2026-09-02T12:00:00", now)
    assert portal_archive.is_within_retention("2026-09-02T06:30:00+00:00", now)
    assert not portal_archive.is_within_retention("2026-09-02T11:59:59", now)


def test_whatsapp_archive_roundtrip_and_deduplication(tmp_path):
    path = tmp_path / "whatsapp_messages.json"
    message = {"timestamp": "2026-10-06T12:00:00+05:30", "sender": "School",
               "text": "Bring your workbook", "attachments": []}
    portal_archive.save_archive([message], path)
    assert portal_archive.merge_into_archive([message], path) == [message]
    assert portal_archive.load_archive(path) == [message]


def test_cleanup_prunes_both_sources_pdfs_and_saved_data(tmp_path, monkeypatch):
    now = datetime(2026, 10, 7, 12, tzinfo=portal_archive.IST)
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    pdf_dir = data_dir / "pdfs"
    pdf_dir.mkdir(parents=True)
    output_dir.mkdir()
    monkeypatch.setattr(portal_archive, "DATA_DIR", data_dir)
    monkeypatch.setattr(portal_archive, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", data_dir / "portal_messages.json")
    monkeypatch.setattr(portal_archive, "WHATSAPP_ARCHIVE_PATH", data_dir / "whatsapp_messages.json")
    monkeypatch.setattr(portal_archive, "LAST_RUN_PATH", data_dir / "last_run.json")
    old_date = (now - timedelta(days=35, seconds=1)).isoformat()
    recent_date = (now - timedelta(days=35)).isoformat()
    old_pdf = pdf_dir / "old.pdf"
    retained_pdf = pdf_dir / "retained.pdf"
    orphan_pdf = pdf_dir / "orphan.pdf"
    recent_orphan = pdf_dir / "recent.pdf"
    outside_pdf = tmp_path / "outside.pdf"
    for path in (old_pdf, retained_pdf, orphan_pdf, recent_orphan, outside_pdf):
        path.write_bytes(b"pdf")
    old = {"id": "old", "posted_at": old_date,
           "attachments": [{"saved_as": str(old_pdf)}, {"saved_as": str(retained_pdf)},
                           {"saved_as": str(outside_pdf)}]}
    recent = {"id": "recent", "posted_at": recent_date,
              "attachments": [{"saved_as": str(retained_pdf)}]}
    old_whatsapp = {"timestamp": old_date, "text": "old", "attachments": []}
    recent_whatsapp = {"timestamp": recent_date, "text": "recent", "attachments": []}
    portal_archive.save_archive([old, recent])
    portal_archive.save_archive([old_whatsapp, recent_whatsapp], portal_archive.WHATSAPP_ARCHIVE_PATH)
    input_path = output_dir / "daily_brief_input.json"
    portal_archive.write_json(input_path, {
        "portal": {"messages": [old, recent], "error": None},
        "whatsapp": {"messages": [old_whatsapp, recent_whatsapp], "error": None},
    })
    stale_output = output_dir / "daily_brief_preview.html"
    stale_output.write_text("stale")
    stale_data = data_dir / "stale.json"
    stale_data.write_text("{}")
    portal_archive.save_last_run(now)
    old_time = (now - timedelta(days=36)).timestamp()
    cache_dir = data_dir / "pdf_cache"
    cache_dir.mkdir()
    import hashlib
    retained_hash = hashlib.sha256(retained_pdf.read_bytes()).hexdigest()
    retained_cache = cache_dir / f"{retained_hash}_v1.json"
    retained_cache.write_text("{}")
    expired_cache = cache_dir / ("0" * 64 + "_v1.json")
    expired_cache.write_text("{}")
    os.utime(retained_cache, (old_time, old_time))
    for path in (retained_pdf, orphan_pdf, stale_output, stale_data, portal_archive.LAST_RUN_PATH):
        os.utime(path, (old_time, old_time))

    stored = portal_archive.cleanup_stored_data(now)

    assert stored == {"portal": [recent], "whatsapp": [recent_whatsapp]}
    assert portal_archive.load_archive() == [recent]
    assert portal_archive.load_archive(portal_archive.WHATSAPP_ARCHIVE_PATH) == [recent_whatsapp]
    assert not old_pdf.exists()
    assert not orphan_pdf.exists()
    assert not stale_output.exists()
    assert not stale_data.exists()
    assert retained_pdf.exists()
    assert recent_orphan.exists()
    assert outside_pdf.exists()
    assert portal_archive.LAST_RUN_PATH.exists()
    assert retained_cache.exists()
    assert not expired_cache.exists()
    saved_input = json.loads(input_path.read_text())
    assert saved_input["portal"]["messages"] == [recent]
    assert saved_input["whatsapp"]["messages"] == [recent_whatsapp]
    assert portal_archive.cleanup_stored_data(now) == stored


def test_load_archive_returns_empty_list_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "portal_messages.json")
    assert portal_archive.load_archive() == []


def test_merge_into_archive_dedupes_by_id(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "portal_messages.json")
    existing = [
        {"id": "1", "title": "old version", "posted_at": "2026-09-01T10:00:00", "body": "b", "attachments": []},
    ]
    (tmp_path / "portal_messages.json").write_text(json.dumps(existing))

    new_messages = [
        {"id": "1", "title": "updated version", "posted_at": "2026-09-01T10:00:00", "body": "b", "attachments": []},
        {"id": "2", "title": "new message", "posted_at": "2026-09-05T10:00:00", "body": "b2", "attachments": []},
    ]

    merged = portal_archive.merge_into_archive(new_messages)

    assert len(merged) == 2
    ids = {m["id"] for m in merged}
    assert ids == {"1", "2"}
    updated = next(m for m in merged if m["id"] == "1")
    assert updated["title"] == "updated version"


def test_merge_into_archive_sorts_most_recent_first(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "portal_messages.json")
    new_messages = [
        {"id": "a", "title": "t", "posted_at": "2026-09-01T10:00:00", "body": "b", "attachments": []},
        {"id": "b", "title": "t", "posted_at": "2026-09-05T10:00:00", "body": "b", "attachments": []},
    ]
    merged = portal_archive.merge_into_archive(new_messages)
    assert [m["id"] for m in merged] == ["b", "a"]


def test_merge_into_archive_does_not_collapse_messages_with_empty_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "portal_messages.json")
    new_messages = [
        {"id": "", "title": "no id one", "posted_at": "2026-09-01T10:00:00", "body": "b", "attachments": []},
        {"id": "", "title": "no id two", "posted_at": "2026-09-05T10:00:00", "body": "b2", "attachments": []},
    ]

    merged = portal_archive.merge_into_archive(new_messages)

    assert len(merged) == 2
    titles = {m["title"] for m in merged}
    assert titles == {"no id one", "no id two"}


def test_save_archive_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(portal_archive, "DATA_DIR", tmp_path)
    monkeypatch.setattr(portal_archive, "ARCHIVE_PATH", tmp_path / "portal_messages.json")
    messages = [{"id": "1", "title": "t", "posted_at": "2026-09-05T10:00:00", "body": "b", "attachments": []}]

    portal_archive.save_archive(messages)

    assert json.loads((tmp_path / "portal_messages.json").read_text()) == messages


def test_is_within_retention_keeps_recent_message():
    now = datetime(2026, 9, 5)
    recent = (now - timedelta(days=10)).isoformat()
    assert portal_archive.is_within_retention(recent, now) is True


def test_is_within_retention_drops_message_older_than_three_months():
    now = datetime(2026, 9, 5)
    old = (now - relativedelta(months=4)).isoformat()
    assert portal_archive.is_within_retention(old, now) is False


def test_prune_archive_keeps_recent_and_drops_old():
    now = datetime(2026, 9, 5)
    recent = {
        "id": "1", "title": "t",
        "posted_at": (now - timedelta(days=5)).isoformat(),
        "body": "b", "attachments": [],
    }
    old = {
        "id": "2", "title": "t",
        "posted_at": (now - relativedelta(months=4)).isoformat(),
        "body": "b", "attachments": [],
    }

    result = portal_archive.prune_archive([recent, old], now)

    assert result == [recent]


def test_prune_archive_deletes_orphaned_pdf(tmp_path):
    now = datetime(2026, 9, 5)
    pdf_path = tmp_path / "2_Homework.pdf"
    pdf_path.write_bytes(b"pdf content")

    old = {
        "id": "2", "title": "t",
        "posted_at": (now - relativedelta(months=4)).isoformat(),
        "body": "b",
        "attachments": [{"name": "Homework", "href": "x", "saved_as": str(pdf_path)}],
    }

    portal_archive.prune_archive([old], now)

    assert not pdf_path.exists()


def test_prune_archive_keeps_pdf_for_recent_message(tmp_path):
    now = datetime(2026, 9, 5)
    pdf_path = tmp_path / "1_Homework.pdf"
    pdf_path.write_bytes(b"pdf content")

    recent = {
        "id": "1", "title": "t",
        "posted_at": (now - timedelta(days=5)).isoformat(),
        "body": "b",
        "attachments": [{"name": "Homework", "href": "x", "saved_as": str(pdf_path)}],
    }

    portal_archive.prune_archive([recent], now)

    assert pdf_path.exists()


def test_prune_archive_handles_empty_id_messages_with_distinct_attachment_paths(tmp_path):
    # Regression test for the scrape_udt.py filename-collision bug: two messages
    # both lacking an "id" (the parser's fallback for an HTML element with no id
    # attribute) used to compute the same saved_as filename if they also shared
    # an attachment display name, so pruning one would delete the other's file
    # too. Post-fix, their saved_as paths are distinct (derived from the
    # attachment's href instead of the empty id) - this asserts prune_archive
    # only touches the pruned message's own file.
    now = datetime(2026, 9, 5)
    old_pdf_path = tmp_path / "_viewer1_Homework.pdf"
    old_pdf_path.write_bytes(b"old pdf content")
    recent_pdf_path = tmp_path / "_viewer2_Homework.pdf"
    recent_pdf_path.write_bytes(b"recent pdf content")

    old = {
        "id": "", "title": "t",
        "posted_at": (now - relativedelta(months=4)).isoformat(),
        "body": "b",
        "attachments": [{"name": "Homework", "href": "/viewer1", "saved_as": str(old_pdf_path)}],
    }
    recent = {
        "id": "", "title": "t",
        "posted_at": (now - timedelta(days=5)).isoformat(),
        "body": "b",
        "attachments": [{"name": "Homework", "href": "/viewer2", "saved_as": str(recent_pdf_path)}],
    }

    result = portal_archive.prune_archive([old, recent], now)

    assert result == [recent]
    assert not old_pdf_path.exists()
    assert recent_pdf_path.exists()
