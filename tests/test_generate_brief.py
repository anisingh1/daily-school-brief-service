import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest
from pydantic import ValidationError

import generate_brief
import portal_archive


@pytest.mark.parametrize("cached", [False, True])
def test_pdf_snapshot_survives_source_and_cache_cleanup(tmp_path, monkeypatch, cached):
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    generate_brief.PDF_DIR.mkdir(parents=True)
    path = generate_brief.PDF_DIR / "worksheet.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "Read page 12")
        document.save(path)
    data = {"portal": {"messages": [{
        "attachments": [{"saved_as": "data/pdfs/worksheet.pdf"}],
    }]}}
    extraction_calls = []

    def extract(*args):
        extraction_calls.append(True)
        return generate_brief.validate_pdf_text(json.dumps({
            "category": "notebook_page", "pages": [{"page": 1, "text": "Read page 12"}],
            "warnings": [],
        }))

    client = Mock(spec=generate_brief.AzureOpenAI)
    monkeypatch.setattr(generate_brief, "extract_pdf_text", extract)
    if cached:
        generate_brief.prepare_input(data, client=client)
        extraction_calls.clear()
    snapshot = generate_brief.snapshot_pdfs(data)
    path.unlink()
    for cache_path in (generate_brief.PDF_DIR.parent / "pdf_cache").glob("*.json"):
        cache_path.unlink()
    cache_writes = {}
    payload, warnings, images = generate_brief.prepare_input(
        data, client=client, pdf_snapshot=snapshot, cache_writes=cache_writes,
    )
    assert warnings == images == []
    assert payload["documents"][0]["pages"] == [{"page": 1, "text": "Read page 12"}]
    assert payload["documents"][0]["category"] == "notebook_page"
    assert len(extraction_calls) == (0 if cached else 1)
    assert len(cache_writes) == (0 if cached else 1)
    assert not list((generate_brief.PDF_DIR.parent / "pdf_cache").glob("*.json"))


def test_failed_atomic_brief_save_preserves_previous_content(tmp_path, monkeypatch):
    path = tmp_path / "brief.json"
    monkeypatch.setattr(generate_brief, "CONTENT_PATH", path)
    previous = generate_brief.empty_brief("previous", [])
    generate_brief.write_content(previous)

    def fail_replace(*args):
        raise OSError("Cannot publish brief")

    monkeypatch.setattr(portal_archive.os, "replace", fail_replace)
    with pytest.raises(OSError, match="Cannot publish brief"):
        generate_brief.write_content(generate_brief.empty_brief("new", []))
    assert generate_brief.BriefContent.model_validate_json(path.read_text()) == previous
    assert list(tmp_path.iterdir()) == [path]


def test_dates_use_ist_even_when_utc_is_previous_day():
    context = generate_brief.date_context(datetime(2026, 10, 6, 20, tzinfo=timezone.utc))
    assert context["today_ist"] == "2026-10-07"
    assert context["today_weekday"] == "Wednesday"
    assert context["include_today_dress_code_reminders"] is True
    assert context["tomorrow_ist"] == "2026-10-08"
    assert context["tomorrow_weekday"] == "Thursday"
    assert context["reminders_through_ist"] == "2026-10-12"
    assert context["brief_date"] == "Thursday, October 8, 2026"


@pytest.mark.parametrize(("hour", "minute", "second", "include_today"), [
    (0, 0, 0, True),
    (2, 59, 59, True),
    (3, 0, 0, False),
    (3, 0, 1, False),
    (4, 30, 0, False),
    (14, 0, 0, False),
])
def test_today_dress_code_stops_at_0830_ist(hour, minute, second, include_today):
    now = datetime(2026, 10, 8, hour, minute, second, tzinfo=timezone.utc)
    payload, _, _ = generate_brief.prepare_input({}, now=now)
    assert payload["today_ist"] == "2026-10-08"
    assert payload["today_weekday"] == "Thursday"
    assert payload["include_today_dress_code_reminders"] is include_today
    assert payload["tomorrow_ist"] == "2026-10-09"


@pytest.mark.parametrize(("hour", "minute", "include_today"), [
    (2, 30, True),
    (3, 0, False),
    (4, 30, False),
])
def test_generation_places_dress_in_dress_code_only(monkeypatch, hour, minute, include_today):
    now = datetime(2026, 10, 8, hour, minute, tzinfo=timezone.utc)

    def create(**kwargs):
        prompt = " ".join(kwargs["messages"][0]["content"].split())
        assert "true only before 8:30 AM IST" in prompt
        assert "Put all dress-related instructions in dress_code only" in prompt
        assert "never include them in reminders" in prompt
        assert "including swimming dress and skating shoes" in prompt
        assert "Keep dress_code itself about tomorrow only" not in prompt
        payload = json.loads(kwargs["messages"][1]["content"][0]["text"])
        assert payload["today_weekday"] == "Thursday"
        assert payload["include_today_dress_code_reminders"] is include_today
        content = generate_brief.empty_brief("date", [])
        content.reminders = ["October 12: School event."]
        if include_today:
            content.dress_code = "Today: Pack skating shoes for Thursday skating."
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=content.model_dump_json()),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    content = generate_brief.generate({"portal": {"messages": [{"body": "School notice"}]}}, now=now)
    if include_today:
        assert content.dress_code == "Today: Pack skating shoes for Thursday skating."
    else:
        assert content.dress_code is None
    assert content.reminders == ["October 12: School event."]


@pytest.mark.parametrize("now, start, end", [
    (datetime(2026, 10, 8, tzinfo=generate_brief.IST), "2026-10-01", "2026-10-31"),
    (datetime(2026, 2, 28, tzinfo=generate_brief.IST), "2026-02-01", "2026-02-28"),
    (datetime(2028, 2, 29, tzinfo=generate_brief.IST), "2028-02-01", "2028-02-29"),
    (datetime(2026, 12, 31, tzinfo=generate_brief.IST), "2026-12-01", "2026-12-31"),
    (datetime(2026, 9, 30, 20, tzinfo=timezone.utc), "2026-10-01", "2026-10-31"),
])
def test_calendar_covers_current_ist_month_even_at_boundaries(monkeypatch, now, start, end):
    monkeypatch.setattr(generate_brief, "create_client", lambda: pytest.fail("Empty sources must not call Azure"))
    context = generate_brief.date_context(now)
    assert context["calendar_start_ist"] == start
    assert context["calendar_end_ist"] == end
    content = generate_brief.generate({}, now=now)
    dates = [day.date.isoformat() for day in content.calendar]
    assert dates[0] == start
    assert dates[-1] == end
    assert len(dates) == int(end[-2:])
    assert len(dates) == len(set(dates))
    assert all(not day.homework and not day.classwork and not day.homework_pages for day in content.calendar)


def test_calendar_normalization_retains_month_events_and_clears_future_assignments(monkeypatch):
    documents = [
        {"category": "notebook_page", "name": "Homework.pdf", "url": "/pdfs/homework.pdf"},
        {"category": "school_notice", "name": "Planner.pdf", "url": "/pdfs/planner.pdf"},
    ]
    now = datetime(2026, 10, 8, 12, tzinfo=generate_brief.IST)
    monkeypatch.setattr(generate_brief, "prepare_input", lambda *_args, **_kwargs: (
        {**generate_brief.date_context(now), "sources": {}, "documents": documents}, [], [],
    ))
    raw = generate_brief.empty_brief("date", []).model_dump(mode="json")

    def day(date, **overrides):
        entry = {"date": date, "classwork": [], "homework": [], "homework_pages": [],
                 "agenda": [], "reminders": [], "holidays": []}
        entry.update(overrides)
        return entry

    raw["calendar"] = [
        day("2026-10-02", holidays=[{"date": "2026-10-02", "reason": "Past planner holiday"}]),
        day("2026-10-07", classwork=["Read chapter 3"], homework=["Complete page 12"], homework_pages=[
            {"name": "Wrong name", "url": "/pdfs/homework.pdf"},
            {"name": "Planner", "url": "/pdfs/planner.pdf"},
        ]),
        day("2026-10-07", homework=["Complete page 12", "Write five words"], homework_pages=[
            {"name": "Duplicate", "url": "/pdfs/homework.pdf"},
        ]),
        day("2026-10-08", classwork=["Today's classwork"], homework=["Today's homework"]),
        day("2026-10-25", classwork=["Invented future classwork"], homework=["Invented future homework"],
            homework_pages=[{"name": "Homework", "url": "/pdfs/homework.pdf"}],
            agenda=["Half-day dismissal at 12:00"], reminders=["Route 4 pickup changes to 07:30"]),
        day("2026-10-30", holidays=[
            {"date": "2026-10-30", "reason": "Planner holiday"},
            {"date": "2026-10-29", "reason": "Wrong date"},
        ]),
        day("2026-11-01", agenda=["Outside current month"]),
    ]

    def create(**kwargs):
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert "calendar" in schema["required"]
        assert set(schema["$defs"]["CalendarDay"]["required"]) == {
            "date", "classwork", "homework", "homework_pages", "agenda", "reminders", "holidays",
        }
        prompt = kwargs["messages"][0]["content"]
        assert "covering the entire current IST month" in prompt
        assert "beyond the five-day window" in prompt
        assert "Do not carry the daily summary's latest homework" in prompt
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=json.dumps(raw)),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    content = generate_brief.generate({"portal": {"messages": [{"body": "Monthly planner"}]}}, now=now)
    dates = [entry.date.isoformat() for entry in content.calendar]
    assert dates == [f"2026-10-{number:02d}" for number in range(1, 32)]
    days = {entry.date.isoformat(): entry for entry in content.calendar}
    assert days["2026-10-07"].homework == ["Complete page 12", "Write five words"]
    assert days["2026-10-07"].classwork == ["Read chapter 3"]
    assert [page.model_dump() for page in days["2026-10-07"].homework_pages] == [
        {"name": "Homework.pdf", "url": "/pdfs/homework.pdf"},
    ]
    assert days["2026-10-08"].homework == ["Today's homework"]
    assert days["2026-10-25"].homework == days["2026-10-25"].classwork == days["2026-10-25"].homework_pages == []
    assert days["2026-10-25"].agenda == ["Half-day dismissal at 12:00"]
    assert days["2026-10-25"].reminders == ["Route 4 pickup changes to 07:30"]
    assert days["2026-10-02"].holidays[0].reason == "Past planner holiday"
    assert [holiday.model_dump(mode="json") for holiday in content.holidays] == [
        {"date": "2026-10-30", "reason": "Planner holiday"},
    ]
    assert days["2026-10-01"].agenda == []
    assert "An unsupported homework PDF link was omitted." in content.warnings


def test_older_saved_briefs_default_to_empty_calendar():
    legacy = generate_brief.empty_brief("date", []).model_dump(mode="json")
    del legacy["calendar"]
    assert generate_brief.BriefContent.model_validate_json(json.dumps(legacy)).calendar == []


def test_holidays_default_to_empty_for_older_saved_briefs():
    legacy = generate_brief.empty_brief("date", []).model_dump(mode="json")
    del legacy["holidays"]
    assert generate_brief.BriefContent.model_validate_json(json.dumps(legacy)).holidays == []


@pytest.mark.parametrize("invalid_date", ["2026-02-30", "tomorrow", "2026-10-09T12:00:00"])
def test_holiday_dates_must_be_real_iso_dates(invalid_date):
    with pytest.raises(ValidationError):
        generate_brief.Holiday.model_validate_json(json.dumps({"date": invalid_date, "reason": "Closure"}))


def test_generation_sorts_deduplicates_and_filters_holidays(monkeypatch):
    raw = generate_brief.empty_brief("date", []).model_dump(mode="json")
    raw["holidays"] = [
        {"date": "2026-11-01", "reason": "Planner holiday beyond reminder window"},
        {"date": "2026-10-09", "reason": "Planner holiday"},
        {"date": "2026-10-07", "reason": "Past holiday"},
        {"date": "2026-10-08", "reason": "Today's holiday"},
        {"date": "2026-10-09", "reason": "Updated closure notice"},
    ]

    def create(**kwargs):
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert "holidays" in schema["required"]
        assert schema["$defs"]["Holiday"]["required"] == ["date", "reason"]
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=json.dumps(raw)),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    content = generate_brief.generate(
        {"portal": {"messages": [{"body": "School notices"}]}},
        now=datetime(2026, 10, 8, 12, tzinfo=generate_brief.IST),
    )
    assert [holiday.model_dump(mode="json") for holiday in content.holidays] == [
        {"date": "2026-10-08", "reason": "Today's holiday"},
        {"date": "2026-10-09", "reason": "Updated closure notice"},
        {"date": "2026-11-01", "reason": "Planner holiday beyond reminder window"},
    ]


@pytest.mark.parametrize("notice, agenda, holidays", [
    ("School closed tomorrow due to rain", "Tomorrow: School closed due to rain.",
     [{"date": "2026-10-09", "reason": "Rain closure"}]),
    ("The October 9 holiday is cancelled; school reopens tomorrow", "Tomorrow: School reopens.", []),
    ("Tomorrow is a half day for Aviraj: dismissal at 12:00, bus drop-off at 12:15",
     "Tomorrow: Half day; dismissal 12:00, bus drop-off 12:15.", []),
    ("Aviraj's bus route 4 pickup tomorrow changes from 07:00 to 07:30",
     "Tomorrow: Route 4 bus pickup changes from 07:00 to 07:30.", []),
])
def test_urgent_notice_prompt_and_response_contract(monkeypatch, notice, agenda, holidays):
    data = {
        "portal": {"messages": [{"posted_at": "2026-10-01T12:00:00+05:30", "body": "Monthly planner"}]},
        "whatsapp": {"messages": [{"timestamp": "2026-10-08T10:00:00+05:30", "text": notice}]},
    }

    def create(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        assert "A newer explicit school notice overrides an older plan" in prompt
        assert "a reopening announcement cancels the earlier holiday" in prompt
        assert "Half days and bus-only changes" in prompt
        assert "Use message" in prompt and "timestamps to resolve relative dates" in prompt
        assert "class, and bus route" in prompt
        payload = json.loads(kwargs["messages"][1]["content"][0]["text"])
        assert payload["sources"] == data
        raw = generate_brief.empty_brief("date", []).model_dump(mode="json")
        raw["agenda"] = [agenda]
        raw["holidays"] = holidays
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=json.dumps(raw)),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    content = generate_brief.generate(data, now=datetime(2026, 10, 8, 12, tzinfo=generate_brief.IST))
    assert content.agenda == [agenda]
    assert [holiday.model_dump(mode="json") for holiday in content.holidays] == holidays


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
    image_part = images[1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
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
            extracted = {"category": "school_notice", "pages": [{"page": 1, "text": "November 1: Aviraj brings a workbook"}], "warnings": []}
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
    monkeypatch.setattr(generate_brief, "PDF_CACHE_VERSION", "test-new-version")
    generate_brief.generate(data)
    assert len(requests) == 9


def test_calendar_holiday_colors_survive_cached_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    generate_brief.PDF_DIR.mkdir(parents=True)
    path = generate_brief.PDF_DIR / "monthly-planner.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "October 2026")
        page.draw_rect(pymupdf.Rect(72, 90, 150, 150), color=(1, 0, 0), fill=(1, 0, 0))
        page.insert_text((80, 110), "9")
        document.save(path)
    data = {"portal": {"messages": [{"attachments": [{"saved_as": str(path)}]}]}}
    holiday = "2026-10-09: School holiday (red calendar-cell background)"
    requests = []

    def create(**kwargs):
        request_type = kwargs["response_format"]["json_schema"]["name"]
        requests.append(request_type)
        prompt = kwargs["messages"][0]["content"]
        if request_type == "pdf_text":
            assert "red-background date cell marks a school holiday" in prompt
            assert "printed month/year and day number" in prompt
            assert "Red text, borders, headings, or decorations alone" in prompt
            assert "flag the uncertainty in warnings" in prompt
            assert any(part["type"] == "image_url" for part in kwargs["messages"][1]["content"])
            content = json.dumps({
                "category": "school_notice", "pages": [{"page": 1, "text": holiday}], "warnings": [],
            })
        else:
            payload = json.loads(kwargs["messages"][1]["content"][0]["text"])
            assert payload["tomorrow_ist"] == "2026-10-09"
            assert payload["documents"][0]["pages"][0]["text"] == holiday
            assert "red-background date cells count" in prompt
            assert "On a confirmed school holiday, omit that day's school dress" in prompt
            brief = generate_brief.empty_brief("date", [])
            brief.agenda = ["Tomorrow: School holiday."]
            brief.holidays = [generate_brief.Holiday.model_validate_json(json.dumps({
                "date": "2026-10-09", "reason": "Red calendar-cell background",
            }))]
            content = brief.model_dump_json()
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=content),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    now = datetime(2026, 10, 8, 12, tzinfo=generate_brief.IST)
    for _ in range(2):
        brief = generate_brief.generate(data, now=now)
        assert brief.agenda == ["Tomorrow: School holiday."]
        assert [holiday.date.isoformat() for holiday in brief.holidays] == ["2026-10-09"]
        assert brief.dress_code is None
    assert requests == ["pdf_text", "school_brief", "school_brief"]


@pytest.mark.parametrize("finish_reason, refusal, pages", [
    ("length", None, [{"page": 1, "text": "partial"}]),
    ("stop", "refused", [{"page": 1, "text": "partial"}]),
    ("stop", None, []),
    ("stop", None, [{"page": 2, "text": "missing first page"}]),
])
def test_pdf_extraction_rejects_incomplete_content(finish_reason, refusal, pages):
    response = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(refusal=refusal, content=json.dumps({"category": "school_notice", "pages": pages, "warnings": []})),
    )])
    client = Mock(spec=generate_brief.AzureOpenAI)
    client.chat = Mock()
    client.chat.completions.create.return_value = response
    with pytest.raises((RuntimeError, ValueError)):
        generate_brief.extract_pdf_text(client, "deployment", [{"page": 1, "text": ""}], [])


@pytest.mark.parametrize("category", ["school_notice", "notebook_page", "unknown"])
def test_pdf_classification_accepts_only_two_categories(category):
    content = json.dumps({"category": category, "pages": [{"page": 1, "text": "School document"}], "warnings": []})
    if category == "unknown":
        with pytest.raises(ValidationError):
            generate_brief.validate_pdf_text(content)
    else:
        assert generate_brief.validate_pdf_text(content).category == category


@pytest.mark.parametrize("category", ["school_notice", "notebook_page"])
def test_collection_caches_classification_and_brief_reuses_homework_links(monkeypatch, tmp_path, category):
    monkeypatch.delenv("SERVICE_PUBLIC_URL", raising=False)
    monkeypatch.setattr(generate_brief, "ROOT", tmp_path)
    monkeypatch.setattr(generate_brief, "PDF_DIR", tmp_path / "data" / "pdfs")
    generate_brief.PDF_DIR.mkdir(parents=True)
    path = generate_brief.PDF_DIR / "document.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "English homework: Trace Ss")
        document.save(path)
    source_url = "https://school.example/document.pdf"
    data = {"portal": {"messages": [{
        "posted_at": "2026-10-07T10:00:00", "body": "Flow of the Day: Recap Ss",
        "attachments": [{"saved_as": "data/pdfs/document.pdf", "source_url": source_url}],
    }]}}

    def create(**kwargs):
        if kwargs["response_format"]["json_schema"]["name"] == "pdf_text":
            assert "Classify the entire PDF into exactly one category" in kwargs["messages"][0]["content"]
            content = json.dumps({
                "category": category, "pages": [{"page": 1, "text": "English homework: Trace Ss"}], "warnings": [],
            })
        else:
            payload = json.loads(kwargs["messages"][1]["content"][0]["text"])
            assert payload["documents"][0]["category"] == category
            assert payload["documents"][0]["message_date"] == "2026-10-07T10:00:00"
            brief = generate_brief.empty_brief("date", [])
            brief.homework = ["English: Trace Ss"]
            if category == "notebook_page":
                brief.homework_pages = [generate_brief.HomeworkPage(name="document.pdf", url="/pdfs/document.pdf")]
            content = brief.model_dump_json()
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(refusal=None, content=content),
        )])

    client = Mock(spec=generate_brief.AzureOpenAI)
    client.chat = Mock()
    client.chat.completions.create.side_effect = create
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    payload, _, _ = generate_brief.prepare_input(data, client=client, deployment="deployment")
    assert payload["documents"][0]["category"] == category
    cache = next((generate_brief.PDF_DIR.parent / "pdf_cache").glob("*.json"))
    assert json.loads(cache.read_text())["category"] == category
    content = generate_brief.generate(data)
    assert client.chat.completions.create.call_count == 2
    assert [page.url for page in content.homework_pages] == (["/pdfs/document.pdf"] if category == "notebook_page" else [])


@pytest.mark.parametrize(("source_url", "base_url", "expected"), [
    ("https://school.example/homework.pdf", "", "/pdfs/Hindi%20worksheet.pdf"),
    ("https://school.example/homework.pdf", "https://brief.example/", "https://brief.example/pdfs/Hindi%20worksheet.pdf"),
    (None, "", "/pdfs/Hindi%20worksheet.pdf"),
    ("javascript:alert(1)", "https://brief.example", "https://brief.example/pdfs/Hindi%20worksheet.pdf"),
])
def test_pdf_links_always_use_encoded_service_url(monkeypatch, source_url, base_url, expected):
    monkeypatch.setenv("SERVICE_PUBLIC_URL", base_url)
    path = generate_brief.PDF_DIR / "Hindi worksheet.pdf"
    assert generate_brief.pdf_link(path, {"source_url": source_url}) == expected


def test_brief_keeps_only_known_notebook_links(monkeypatch):
    documents = [
        {"category": "notebook_page", "name": "Hindi.pdf", "url": "https://school.example/hindi.pdf"},
        {"category": "school_notice", "name": "Assembly.pdf", "url": "https://school.example/assembly.pdf"},
    ]
    monkeypatch.setattr(generate_brief, "prepare_input", lambda *_args, **_kwargs: (
        {"brief_date": "Friday, October 9, 2026", "today_ist": "2026-10-08", "documents": documents}, [], [],
    ))
    content = generate_brief.empty_brief("date", [])
    content.homework_pages = [
        generate_brief.HomeworkPage(name="Wrong name", url=documents[0]["url"]),
        generate_brief.HomeworkPage(name="Duplicate", url=documents[0]["url"]),
        generate_brief.HomeworkPage(name="Assembly", url=documents[1]["url"]),
        generate_brief.HomeworkPage(name="Invented", url="https://unknown.example/fake.pdf"),
    ]
    client = Mock(spec=generate_brief.AzureOpenAI)
    client.chat = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(refusal=None, content=content.model_dump_json()),
    )])
    monkeypatch.setattr(generate_brief, "create_client", lambda: (client, "deployment"))
    result = generate_brief.generate({"portal": {"messages": [{"body": "Homework"}]}})
    assert [page.model_dump() for page in result.homework_pages] == [
        {"name": "Hindi.pdf", "url": "https://school.example/hindi.pdf"},
    ]
    assert result.warnings == ["An unsupported homework PDF link was omitted."]
    schema = client.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["schema"]
    assert "homework_pages" in schema["required"]


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