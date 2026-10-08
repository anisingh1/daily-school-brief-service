import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pymupdf
from pydantic import ValidationError

import generate_brief


def test_dates_use_ist_even_when_utc_is_previous_day():
    context = generate_brief.date_context(datetime(2026, 10, 6, 20, tzinfo=timezone.utc))
    assert context["today_ist"] == "2026-10-07"
    assert context["tomorrow_ist"] == "2026-10-08"
    assert context["tomorrow_weekday"] == "Thursday"
    assert context["reminders_through_ist"] == "2026-10-12"
    assert context["brief_date"] == "Thursday, October 8, 2026"


def test_schema_rejects_wrong_types_and_extra_keys():
    data = generate_brief.empty_brief("date", []).model_dump()
    data["homework"] = "not a list"
    with pytest.raises(ValidationError):
        generate_brief.BriefContent.model_validate(data)
    data["homework"] = []
    data["extra"] = "unexpected"
    with pytest.raises(ValidationError):
        generate_brief.BriefContent.model_validate(data)


def test_empty_sources_do_not_call_azure(monkeypatch):
    monkeypatch.setattr(generate_brief, "create_client", lambda: pytest.fail("Unexpected Azure call"))
    content = generate_brief.generate({
        "portal": {"messages": [], "error": "login failed"},
        "whatsapp": {"messages": [], "error": None},
    })
    assert content.warnings == ["portal: login failed"]
    assert content.homework == []


def test_pdf_text_and_errors_are_included_without_changing_sources(monkeypatch, tmp_path):
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    pdf_dir = tmp_path / "data" / "pdfs"
    pdf_dir.mkdir(parents=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Thursday: Aviraj - word of the day")
        document.new_page()
        document.save(pdf_dir / "planner.pdf")
    data = {"portal": {"messages": [{"attachments": [
        {"saved_as": "data/pdfs/planner.pdf"},
        {"saved_as": "data/pdfs/planner.pdf"},
        {"saved_as": "../../private.pdf"},
    ]}], "error": None}}
    before = json.dumps(data)
    payload, warnings, images = generate_brief.prepare_input(data)
    assert len(payload["documents"]) == 1
    assert "Aviraj" in payload["documents"][0]["pages"][0]["text"]
    assert len(images) == 4
    assert images[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert payload["documents"][0]["pages"][1]["text"] == ""
    assert warnings == ["Could not read attachment private.pdf (ValueError)."]
    assert json.dumps(data) == before


def test_generation_reuses_pdf_cache_and_invalidates_changed_content(monkeypatch, tmp_path):
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    pdf_dir = generate_brief.PDF_DIR
    pdf_dir.mkdir(parents=True)
    pdf_path = pdf_dir / "planner.pdf"

    def save_pdf(text):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), text)
            document.save(pdf_path)

    save_pdf("November 1: Aviraj brings a workbook")
    data = {"portal": {"messages": [{"body": "Planner", "attachments": [
        {"saved_as": str(pdf_path)},
    ]}], "error": None}}
    requests = []
    brief = generate_brief.empty_brief("date", []).model_dump()

    def create(**kwargs):
        requests.append(kwargs)
        if kwargs["response_format"]["json_schema"]["name"] == "pdf_text":
            assert any(part["type"] == "image_url" for part in kwargs["messages"][1]["content"])
            extracted = {"pages": [{"page": 1, "text": "November 1: Aviraj brings a workbook"}], "warnings": []}
            content = json.dumps(extracted)
        else:
            assert all(part["type"] == "text" for part in kwargs["messages"][1]["content"])
            assert "November 1" in kwargs["messages"][1]["content"][0]["text"]
            content = json.dumps(brief)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=content),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    generate_brief.generate(data)
    generate_brief.generate(data)
    assert [request["response_format"]["json_schema"]["name"] for request in requests] == [
        "pdf_text", "school_brief", "school_brief",
    ]
    cache = next((pdf_dir.parent / "pdf_cache").glob("*.json"))
    cache.write_text("broken JSON")
    generate_brief.generate(data)
    assert len(requests) == 5
    pdf_path.unlink()
    save_pdf("Changed planner")
    generate_brief.generate(data)
    assert len(requests) == 7
    monkeypatch.setattr(generate_brief, "PDF_CACHE_VERSION", "v2")
    generate_brief.generate(data)
    assert len(requests) == 9


@pytest.mark.parametrize("finish_reason, refusal, pages", [
    ("length", None, [{"page": 1, "text": "partial"}]),
    ("stop", "refused", [{"page": 1, "text": "partial"}]),
    ("stop", None, []),
    ("stop", None, [{"page": 2, "text": "missing first page"}]),
])
def test_pdf_extraction_rejects_incomplete_content(finish_reason, refusal, pages):
    response = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(refusal=refusal, content=json.dumps({"pages": pages, "warnings": []})),
    )])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    with pytest.raises((RuntimeError, ValueError)):
        generate_brief.extract_pdf_text(client, "deployment", [{"page": 1, "text": ""}], [])


@pytest.mark.parametrize("finish_reason, refusal, invalid", [
    ("stop", None, False), ("length", None, False),
    ("stop", "refused", False), ("stop", None, True),
])
def test_azure_generation_validates_response_and_closes_client(monkeypatch, finish_reason, refusal, invalid):
    content = generate_brief.empty_brief("wrong date", []).model_dump()
    content["homework"] = "invalid" if invalid else ["Read page 12"]
    calls = {}
    def create(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(refusal=refusal, content=json.dumps(content)),
        )])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=lambda: calls.update(closed=True),
    )
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "configured-deployment"))
    data = {"portal": {"messages": [{"body": "Recap: read page 12"}], "error": None}}
    if finish_reason != "stop" or refusal or invalid:
        with pytest.raises((RuntimeError, ValidationError)):
            generate_brief.generate(data)
    else:
        result = generate_brief.generate(data, datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
        assert result.homework == ["Read page 12"]
        assert result.date == "Thursday, October 8, 2026"
    assert calls["closed"]
    assert calls["model"] == "configured-deployment"
    assert calls["response_format"]["json_schema"]["strict"] is True