import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pymupdf
import pytest

import generate_brief


@pytest.fixture
def cached_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    monkeypatch.setattr(generate_brief, "CONTENT_PATH", tmp_path / "output" / "daily_brief_content.json")
    generate_brief.PDF_DIR.mkdir(parents=True)
    now = datetime(2026, 10, 15, 12, tzinfo=generate_brief.IST)
    data = {
        "portal": {"messages": [{"body": "Read page 12", "attachments": []}], "error": None},
        "whatsapp": {"messages": [], "error": None},
        "attendance": {"messages": [], "records": [], "error": None},
    }
    calls = []

    def create(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        calls.append(name)
        content = generate_brief.empty_brief("date", []).model_dump_json()
        if name == "pdf_text":
            content = json.dumps({
                "category": "notebook_page", "pages": [{"page": 1, "text": "Read page 12"}],
                "warnings": [],
            })
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=content),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    return data, now, calls


def add_pdf(data, text="Read page 12"):
    path = generate_brief.PDF_DIR / "worksheet.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), text)
        document.save(path)
    data["portal"]["messages"][0]["attachments"] = [{"saved_as": str(path)}]
    return path


def test_unchanged_run_reuses_persistent_content_without_creating_azure_client(cached_pipeline, monkeypatch):
    data, now, calls = cached_pipeline
    original, reused = generate_brief.run_with_status(data, now)
    assert not reused
    path = generate_brief.CONTENT_PATH
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(generate_brief, "create_client", lambda: pytest.fail("Cache hit must not create Azure client"))
    monkeypatch.setattr(generate_brief, "load_stored_input", lambda now: deepcopy(data))
    assert generate_brief.run(now=now + timedelta(minutes=30)) == original
    assert generate_brief.run_with_status(deepcopy(data), now)[1] is True
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert calls == ["school_brief"]


@pytest.mark.parametrize("change", [
    "message", "error", "attendance", "prompt", "pdf_version",
    "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION", "SERVICE_PUBLIC_URL",
    "AZURE_OPENAI_PDF_DEPLOYMENT", "AZURE_OPENAI_BRIEF_DEPLOYMENT",
])
def test_relevant_changes_invalidate_brief(cached_pipeline, monkeypatch, change):
    data, now, calls = cached_pipeline
    generate_brief.run(data, now)
    if change == "message":
        data["portal"]["messages"][0]["body"] = "Read page 13"
    elif change == "error":
        data["whatsapp"]["error"] = "Connection unavailable"
    elif change == "attendance":
        data["attendance"]["records"] = [{"date": "2026-10-15", "status": "present"}]
    elif change == "prompt":
        monkeypatch.setattr(generate_brief, "SYSTEM_PROMPT", generate_brief.SYSTEM_PROMPT + "\nNew rule.")
    elif change == "pdf_version":
        monkeypatch.setattr(generate_brief, "PDF_CACHE_VERSION", "next")
    else:
        monkeypatch.setenv(change, "https://changed.test" if "URL" in change or "ENDPOINT" in change else "changed")
    content, reused = generate_brief.run_with_status(data, now)
    assert not reused
    assert calls == ["school_brief", "school_brief"]
    if change == "attendance":
        assert next(day for day in content.calendar if day.date.isoformat() == "2026-10-15").attendance == "present"


@pytest.mark.parametrize(("start", "end", "expected_reuse"), [
    ("2026-10-15T08:00:00", "2026-10-15T08:29:59", True),
    ("2026-10-15T08:29:59", "2026-10-15T08:30:00", False),
    ("2026-10-15T08:30:00", "2026-10-15T23:59:59", True),
    ("2026-10-15T23:59:59", "2026-10-16T00:00:00", False),
    ("2026-10-31T23:59:59", "2026-11-01T00:00:00", False),
])
def test_date_context_controls_reuse(cached_pipeline, start, end, expected_reuse):
    data, _, calls = cached_pipeline
    generate_brief.run(data, datetime.fromisoformat(start).replace(tzinfo=generate_brief.IST))
    _, reused = generate_brief.run_with_status(data, datetime.fromisoformat(end).replace(tzinfo=generate_brief.IST))
    assert reused is expected_reuse
    assert len(calls) == (1 if expected_reuse else 2)


@pytest.mark.parametrize("damage", ["missing", "invalid", "changed"])
def test_pdf_cache_changes_invalidate_brief(cached_pipeline, damage):
    data, now, calls = cached_pipeline
    add_pdf(data)
    generate_brief.run(data, now)
    assert generate_brief.run_with_status(data, now)[1] is True
    assert calls == ["pdf_text", "school_brief"]
    cache = next((generate_brief.PDF_DIR.parent / "pdf_cache").glob("*.json"))
    if damage == "missing":
        cache.unlink()
    elif damage == "invalid":
        cache.write_text("invalid")
    else:
        extracted = json.loads(cache.read_text())
        extracted["pages"][0]["text"] = "Corrected transcription"
        cache.write_text(json.dumps(extracted))
    assert generate_brief.run_with_status(data, now)[1] is False
    assert calls == ["pdf_text", "school_brief"] + (["school_brief"] if damage == "changed" else ["pdf_text", "school_brief"])
    assert generate_brief.run_with_status(data, now)[1] is True


def test_changed_pdf_bytes_invalidate_even_when_path_is_unchanged(cached_pipeline):
    data, now, calls = cached_pipeline
    path = add_pdf(data)
    generate_brief.run(data, now)
    path.unlink()
    add_pdf(data, "Changed homework")
    assert generate_brief.run_with_status(data, now)[1] is False
    assert generate_brief.run_with_status(data, now)[1] is True
    assert calls == ["pdf_text", "school_brief", "pdf_text", "school_brief"]


@pytest.mark.parametrize("setting", ["AZURE_OPENAI_PDF_DEPLOYMENT", "AZURE_OPENAI_BRIEF_DEPLOYMENT"])
def test_deployment_change_invalidates_brief_but_reuses_pdf_cache(cached_pipeline, monkeypatch, setting):
    data, now, calls = cached_pipeline
    add_pdf(data)
    generate_brief.run(data, now)
    monkeypatch.setenv(setting, "new-deployment")
    assert generate_brief.run_with_status(data, now)[1] is False
    assert generate_brief.run_with_status(data, now)[1] is True
    assert calls == ["pdf_text", "school_brief", "school_brief"]


@pytest.mark.parametrize("damage", ["metadata-missing", "metadata-invalid", "brief-missing", "brief-invalid", "brief-edited"])
def test_missing_or_mismatched_saved_content_cannot_be_a_hit(cached_pipeline, damage):
    data, now, calls = cached_pipeline
    generate_brief.run(data, now)
    path = generate_brief.CONTENT_PATH
    if damage.startswith("metadata"):
        path = path.with_suffix(".cache.json")
    if damage.endswith("missing"):
        path.unlink()
    elif damage.endswith("invalid"):
        path.write_text("invalid")
    else:
        edited = json.loads(path.read_text())
        edited["homework"] = ["Manually edited"]
        path.write_text(json.dumps(edited))
    assert generate_brief.run_with_status(data, now)[1] is False
    assert calls == ["school_brief", "school_brief"]


def test_failed_generation_preserves_brief_and_metadata_and_retries(cached_pipeline, monkeypatch):
    data, now, calls = cached_pipeline
    generate_brief.run(data, now)
    path = generate_brief.CONTENT_PATH
    metadata = path.with_suffix(".cache.json")
    original = path.read_bytes(), metadata.read_bytes()
    data["portal"]["messages"][0]["body"] = "Changed"
    generate = generate_brief.generate

    def fail(*args, **kwargs):
        raise RuntimeError("Azure unavailable")

    monkeypatch.setattr(generate_brief, "generate", fail)
    with pytest.raises(RuntimeError, match="Azure unavailable"):
        generate_brief.run(data, now)
    assert (path.read_bytes(), metadata.read_bytes()) == original
    monkeypatch.setattr(generate_brief, "generate", generate)
    assert generate_brief.run_with_status(data, now)[1] is False
    assert calls == ["school_brief", "school_brief"]


def test_missing_pdf_does_not_freeze_partial_brief_in_cache(cached_pipeline):
    data, now, calls = cached_pipeline
    path = add_pdf(data)
    path.unlink()
    generate_brief.run(data, now)
    assert not generate_brief.CONTENT_PATH.with_suffix(".cache.json").exists()
    add_pdf(data)
    assert generate_brief.run_with_status(data, now)[1] is False
    assert generate_brief.run_with_status(data, now)[1] is True
    assert calls == ["school_brief", "pdf_text", "school_brief"]


def test_failed_extraction_is_retried_not_cached_as_complete(cached_pipeline, monkeypatch):
    data, now, calls = cached_pipeline
    add_pdf(data)
    extract = generate_brief.extract_pdf_text

    def fail(*args):
        raise RuntimeError("Incomplete extraction")

    monkeypatch.setattr(generate_brief, "extract_pdf_text", fail)
    generate_brief.run(data, now)
    assert not generate_brief.CONTENT_PATH.with_suffix(".cache.json").exists()
    monkeypatch.setattr(generate_brief, "extract_pdf_text", extract)
    assert generate_brief.run_with_status(data, now)[1] is False
    assert generate_brief.run_with_status(data, now)[1] is True
    assert calls == ["school_brief", "pdf_text", "school_brief"]
