# Always-On Pi Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained `pi_service/` package that polls the school portal (60 min) and WhatsApp (10 min) for new content, regenerates the daily brief via a direct Anthropic API call only when something changed (or once/day as a date-correctness backstop), and serves the latest brief on a LAN-only Flask page — without touching any existing root-level file.

**Architecture:** `pi_service/` duplicates the root project's fetcher logic (`scrape_udt.py`, `fetch_whatsapp.py`, `cutoff.py`, `portal_archive.py` — copied, one import fixed) and adds four new pieces: `generate_brief.py` (direct Anthropic API call replacing the Claude Code skill), `render_brief.py` (adapted from `render_email.py` for a web page instead of an email), and `service.py` (the always-on entrypoint: two poll loops + a daily backstop loop + a Flask app, all in one process, guarded by a lock). See `docs/superpowers/specs/2026-09-09-always-on-service-design.md` for the full design and rationale.

**Tech Stack:** Python 3.11, `anthropic` (direct API, `messages.parse` structured outputs), Flask (built-in dev server, LAN-only), `requests`/`beautifulsoup4`/`python-dateutil`/`python-dotenv` (carried over from the root project), pytest.

---

## Before you start

All commands below assume your shell's current directory is the repo root (`/Users/anisingh/GitHub/daily-school-brief`). `sed -i ''` is the macOS/BSD form used throughout (matches this repo's dev machine) — on Linux use `sed -i` (no empty string argument).

---

### Task 1: `pi_service/` package skeleton

**Files:**
- Create: `pi_service/__init__.py`
- Create: `pi_service/tests/__init__.py`
- Create: `pi_service/.gitignore`
- Create: `pi_service/requirements.txt`
- Create: `pi_service/.env.example`

- [ ] **Step 1: Create the directories and empty package markers**

```bash
mkdir -p pi_service/tests
touch pi_service/__init__.py
touch pi_service/tests/__init__.py
```

`pi_service/tests/__init__.py` matters beyond marking a package: with both `pi_service/__init__.py` and `pi_service/tests/__init__.py` present, pytest's default import mode walks upward through both `__init__.py`-having directories and inserts the **repo root** onto `sys.path` — which is exactly what lets `pi_service/tests/*.py` do `import pi_service.cutoff` and get *this* package, not get confused with the root-level `cutoff.py` module of the same base name.

- [ ] **Step 2: Write `pi_service/.gitignore`**

```
.env
data/
output/
__pycache__/
*.pyc
.venv/
venv/
```

This is scoped to `pi_service/` only (git supports nested `.gitignore` files) — the root `.gitignore` is left untouched.

- [ ] **Step 3: Write `pi_service/requirements.txt`**

```
requests>=2.31.0
beautifulsoup4>=4.12.0
python-dateutil>=2.8.2
python-dotenv>=1.0.0
pytest>=7.4.0
anthropic>=1.0.0
pydantic>=2.0.0
flask>=3.0.0
tzdata>=2024.1
```

- [ ] **Step 4: Write `pi_service/.env.example`**

```
# Copy this file to pi_service/.env and fill in your real values.
# This is a separate .env from the root project's - pi_service is fully
# self-contained and does not read the root .env.

UDT_BASE_URL=https://sarvottam.udtweb.com
UDT_USERNAME=your_username_here
UDT_PASSWORD=your_password_here

# WhatsApp capture (see ../docs/PHONE_SETUP.md) - same Gmail account/app
# password pattern as the root project's send_email.py setup.
SMTP_USERNAME=your_gmail_address@gmail.com
SMTP_APP_PASSWORD=your_16_character_app_password
WHATSAPP_EMAIL_TO=your_gmail_address+whatsapp@gmail.com

# Anthropic API key for direct brief generation (see generate_brief.py).
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Port for the local web page (LAN-only, no auth).
PORT=8765
```

- [ ] **Step 5: Commit**

```bash
git add pi_service/__init__.py pi_service/tests/__init__.py pi_service/.gitignore pi_service/requirements.txt pi_service/.env.example
git commit -m "pi_service: package skeleton, requirements, env example"
```

---

### Task 2: Copy `cutoff.py` (verbatim)

**Files:**
- Create: `pi_service/cutoff.py` (copy of `cutoff.py`)
- Create: `pi_service/tests/test_cutoff.py` (copy of `tests/test_cutoff.py`, import fixed)

- [ ] **Step 1: Copy the file — no code changes needed**

```bash
cp cutoff.py pi_service/cutoff.py
```

`cutoff.py` has no repo-internal imports and no file paths, so it works identically from its new location.

- [ ] **Step 2: Copy and fix the test file's import**

```bash
cp tests/test_cutoff.py pi_service/tests/test_cutoff.py
sed -i '' 's/^import cutoff$/from pi_service import cutoff/' pi_service/tests/test_cutoff.py
```

- [ ] **Step 3: Run the copied tests**

```bash
python -m pytest pi_service/tests/test_cutoff.py -v
```
Expected: 4 passed (same assertions as the root suite, now against `pi_service.cutoff`).

- [ ] **Step 4: Commit**

```bash
git add pi_service/cutoff.py pi_service/tests/test_cutoff.py
git commit -m "pi_service: copy cutoff.py verbatim"
```

---

### Task 3: Copy `scrape_udt.py` (verbatim)

**Files:**
- Create: `pi_service/scrape_udt.py` (copy of `scrape_udt.py`)
- Create: `pi_service/tests/test_scrape_udt.py` (copy of `tests/test_scrape_udt.py`, imports fixed)

- [ ] **Step 1: Copy the file — no code changes needed**

```bash
cp scrape_udt.py pi_service/scrape_udt.py
```

Every path `scrape_udt.py` touches (`OUTPUT_DIR`, `PDF_DIR`) is derived from `Path(__file__).parent`, so the copy automatically reads/writes under `pi_service/output/` and `pi_service/data/pdfs/` with zero edits.

- [ ] **Step 2: Copy and fix the test file's imports**

```bash
cp tests/test_scrape_udt.py pi_service/tests/test_scrape_udt.py
sed -i '' \
  -e 's/from scrape_udt import/from pi_service.scrape_udt import/' \
  -e 's/^\( *\)import scrape_udt$/\1import pi_service.scrape_udt as scrape_udt/' \
  pi_service/tests/test_scrape_udt.py
```

- [ ] **Step 3: Run the copied tests**

```bash
python -m pytest pi_service/tests/test_scrape_udt.py -v
```
Expected: all tests pass (same as the root suite, now against `pi_service.scrape_udt`).

- [ ] **Step 4: Commit**

```bash
git add pi_service/scrape_udt.py pi_service/tests/test_scrape_udt.py
git commit -m "pi_service: copy scrape_udt.py verbatim"
```

---

### Task 4: Copy `portal_archive.py` (one import fixed)

**Files:**
- Create: `pi_service/portal_archive.py` (copy of `portal_archive.py`, one line changed)
- Create: `pi_service/tests/test_portal_archive.py` (copy of `tests/test_portal_archive.py`, imports fixed)

- [ ] **Step 1: Copy the file**

```bash
cp portal_archive.py pi_service/portal_archive.py
```

- [ ] **Step 2: Fix the bare `cutoff` import**

`portal_archive.py` has `from cutoff import month_anchor` — a bare, unqualified import. Left as-is in the copy, this would resolve to whichever `cutoff` module `sys.path` finds first, risking a silent dependency on the *root's* `cutoff.py` instead of `pi_service`'s own copy (and breaking entirely if `pi_service/` is ever deployed standalone, without the root scripts alongside it). Fix it to import the package-qualified copy:

```bash
sed -i '' 's/^from cutoff import month_anchor$/from pi_service.cutoff import month_anchor/' pi_service/portal_archive.py
```

- [ ] **Step 3: Copy and fix the test file's imports**

```bash
cp tests/test_portal_archive.py pi_service/tests/test_portal_archive.py
sed -i '' \
  -e 's/^import portal_archive$/import pi_service.portal_archive as portal_archive/' \
  -e 's/from cutoff import month_anchor/from pi_service.cutoff import month_anchor/' \
  pi_service/tests/test_portal_archive.py
```

- [ ] **Step 4: Run the copied tests**

```bash
python -m pytest pi_service/tests/test_portal_archive.py -v
```
Expected: all tests pass, including `test_compute_portal_cutoff_falls_back_to_month_anchor_when_absent` (confirms the import fix works).

- [ ] **Step 5: Commit**

```bash
git add pi_service/portal_archive.py pi_service/tests/test_portal_archive.py
git commit -m "pi_service: copy portal_archive.py, fix cutoff import to package-qualified"
```

---

### Task 5: Copy `fetch_whatsapp.py` (verbatim, no cursor)

**Files:**
- Create: `pi_service/fetch_whatsapp.py` (copy of `fetch_whatsapp.py`)
- Create: `pi_service/tests/test_fetch_whatsapp.py` (copy of `tests/test_fetch_whatsapp.py`, imports fixed)

- [ ] **Step 1: Copy the file — no code changes needed**

```bash
cp fetch_whatsapp.py pi_service/fetch_whatsapp.py
```

No cursor is added here (see the design doc's "why not" section) — `pi_service/service.py` (Task 8) will call `fetch_recent_whatsapp_messages(cutoff=month_anchor(months_back=3))` on every poll, exactly like `daily_brief.py` does today, and detect new content by diffing results rather than by an incremental fetch.

- [ ] **Step 2: Copy and fix the test file's imports**

```bash
cp tests/test_fetch_whatsapp.py pi_service/tests/test_fetch_whatsapp.py
sed -i '' \
  -e 's/from fetch_whatsapp import/from pi_service.fetch_whatsapp import/' \
  -e 's/^\( *\)import fetch_whatsapp$/\1import pi_service.fetch_whatsapp as fetch_whatsapp/' \
  pi_service/tests/test_fetch_whatsapp.py
```

- [ ] **Step 3: Run the copied tests**

```bash
python -m pytest pi_service/tests/test_fetch_whatsapp.py -v
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add pi_service/fetch_whatsapp.py pi_service/tests/test_fetch_whatsapp.py
git commit -m "pi_service: copy fetch_whatsapp.py verbatim (no cursor)"
```

---

### Task 6: `render_brief.py` (adapted from `render_email.py`)

**Files:**
- Create: `pi_service/render_brief.py`
- Create: `pi_service/tests/test_render_brief.py`

- [ ] **Step 1: Write the test file (existing rendering tests + two new ones for the banner)**

```python
from pi_service.render_brief import render_brief_html


def _base_data(**overrides):
    data = {
        "date": "2026-09-05",
        "warnings": [],
        "aviraj_highlight": None,
        "homework": [],
        "agenda": [],
        "dress_code": None,
        "reminders": [],
    }
    data.update(overrides)
    return data


def test_render_includes_homework_section_when_present():
    html = render_brief_html(_base_data(homework=["Read pages 10-12"]))
    assert "Homework" in html
    assert "Read pages 10-12" in html


def test_render_omits_empty_sections():
    html = render_brief_html(_base_data(agenda=["Skill Analysis day"]))
    assert "Homework" not in html
    assert "Dress Code" not in html
    assert "Skill Analysis day" in html


def test_render_shows_nothing_new_message_when_all_empty():
    html = render_brief_html(_base_data())
    assert "Nothing new" in html


def test_render_includes_warnings_banner():
    html = render_brief_html(_base_data(warnings=["WhatsApp fetch failed"]))
    assert "WhatsApp fetch failed" in html


def test_render_includes_aviraj_highlight():
    html = render_brief_html(
        _base_data(aviraj_highlight="Aviraj is presenting show and tell tomorrow")
    )
    assert "Aviraj is presenting show and tell tomorrow" in html


def test_render_includes_dress_code_section_when_present():
    html = render_brief_html(
        _base_data(dress_code="Swimming dress (Tuesday is swim day)")
    )
    assert "Dress Code" in html
    assert "Swimming dress (Tuesday is swim day)" in html


def test_render_escapes_html_special_characters():
    html = render_brief_html(_base_data(homework=["Read <Chapter 3> & write notes"]))
    assert "<Chapter 3>" not in html
    assert "&lt;Chapter 3&gt;" in html


def test_render_normalizes_bare_string_list_field_to_single_item():
    html = render_brief_html(_base_data(homework="Read pages 10-12"))
    assert html.count("<li") == 1
    assert "Read pages 10-12" in html


def test_render_omits_homework_section_when_all_items_blank():
    html = render_brief_html(_base_data(homework=[""]))
    assert "Homework" not in html


def test_render_omits_dress_code_section_when_whitespace_only():
    html = render_brief_html(_base_data(dress_code="   "))
    assert "Dress Code" not in html


def test_render_shows_nothing_new_message_when_all_fields_blank_variants():
    html = render_brief_html(
        _base_data(
            warnings=[""],
            aviraj_highlight="   ",
            homework=["  "],
            agenda=[],
            dress_code="",
            reminders=[""],
        )
    )
    assert "Nothing new" in html


def test_render_includes_banner_html_when_given():
    html = render_brief_html(_base_data(), banner_html="<div>Last attempt failed</div>")
    assert "Last attempt failed" in html


def test_render_omits_banner_wrapper_when_not_given():
    html = render_brief_html(_base_data())
    assert "Last attempt failed" not in html
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
python -m pytest pi_service/tests/test_render_brief.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'pi_service.render_brief'`.

- [ ] **Step 3: Write `pi_service/render_brief.py`**

```python
"""
Renders the daily school brief's structured content into a polished,
section-based HTML page - adapted from the root project's
render_email.py to serve the pi_service web page instead of an email
body. Takes the same structured dict shape produced by generate_brief.py
(mirroring the original Claude Code skill's categorization output), so
visual design is deterministic and consistent regardless of that day's
content.

Expected `data` keys: `date` (str), `warnings` (list[str]),
`aviraj_highlight` (str | None), `classwork` (list[str]), `homework`
(list[str]), `agenda` (list[str]), `dress_code` (str | None),
`reminders` (list[str]).
"""

from html import escape


def _as_list(value) -> list[str]:
    """Normalize a list-typed field that may arrive as a bare string.

    generate_brief.py's output is LLM-produced, so a single-item field
    could plausibly come out as a bare string instead of a single-element
    list. Iterating a bare string directly would silently produce one
    <li> per character, so callers should always route list-typed fields
    through this helper first.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


_COLORS = {
    "header_bg": "#4338CA",
    "header_text": "#FFFFFF",
    "page_bg": "#F3F4F6",
    "card_bg": "#FFFFFF",
    "text": "#1F2937",
    "muted": "#6B7280",
    "warning_bg": "#FEF3C7",
    "warning_border": "#F59E0B",
    "highlight_bg": "#FEF9C3",
    "highlight_border": "#CA8A04",
    "classwork_accent": "#0891B2",
    "homework_accent": "#2563EB",
    "agenda_accent": "#7C3AED",
    "dress_accent": "#EA580C",
    "reminders_accent": "#059669",
}


def _section(title: str, accent: str, items: list[str]) -> str:
    items = [item for item in items if item and item.strip()]
    if not items:
        return ""
    rows = "".join(
        f'<li style="margin: 0 0 8px 0; line-height: 1.5;">{escape(item)}</li>'
        for item in items
    )
    return f"""
    <div style="margin: 0 0 24px 0; padding: 16px 20px; background: #FAFAFA;
                border-left: 4px solid {accent}; border-radius: 4px;">
      <h2 style="margin: 0 0 8px 0; font-size: 16px; color: {_COLORS['text']};">
        {escape(title)}
      </h2>
      <ul style="margin: 0; padding-left: 20px;">{rows}</ul>
    </div>
    """


def render_brief_html(data: dict, banner_html: str = "") -> str:
    """Render `data` into a full HTML page.

    `banner_html`, if given, is raw HTML inserted at the very top of the
    page body (above the brief card) - used by the pi_service web page to
    show a "last regeneration attempt failed" notice above the last
    successfully-generated brief, without changing the default rendering
    when left blank.
    """
    date = data.get("date", "")
    warnings = [w for w in _as_list(data.get("warnings")) if w and w.strip()]
    aviraj_highlight = data.get("aviraj_highlight")
    aviraj_highlight = (
        aviraj_highlight if aviraj_highlight and aviraj_highlight.strip() else None
    )
    classwork = _as_list(data.get("classwork"))
    homework = _as_list(data.get("homework"))
    agenda = _as_list(data.get("agenda"))
    dress_code = data.get("dress_code")
    dress_code = dress_code if dress_code and dress_code.strip() else None
    reminders = _as_list(data.get("reminders"))

    warning_html = ""
    if warnings:
        items = "".join(f"<li>{escape(w)}</li>" for w in warnings)
        warning_html = f"""
        <div style="margin: 0 0 20px 0; padding: 12px 16px; background: {_COLORS['warning_bg']};
                    border-left: 4px solid {_COLORS['warning_border']}; border-radius: 4px;
                    font-size: 14px; color: {_COLORS['text']};">
          <strong>⚠️ Heads up:</strong>
          <ul style="margin: 4px 0 0 0; padding-left: 20px;">{items}</ul>
        </div>
        """

    highlight_html = ""
    if aviraj_highlight:
        highlight_html = f"""
        <div style="margin: 0 0 20px 0; padding: 16px 20px; background: {_COLORS['highlight_bg']};
                    border-left: 4px solid {_COLORS['highlight_border']}; border-radius: 4px;
                    font-size: 15px; font-weight: 600; color: {_COLORS['text']};">
          ⭐ {escape(aviraj_highlight)}
        </div>
        """

    dress_html = ""
    if dress_code:
        dress_html = _section("👕 Dress Code", _COLORS["dress_accent"], [dress_code])

    body = "".join([
        warning_html,
        highlight_html,
        _section("🏫 Classwork", _COLORS["classwork_accent"], classwork),
        _section("📚 Homework", _COLORS["homework_accent"], homework),
        _section("📅 Tomorrow's Agenda", _COLORS["agenda_accent"], agenda),
        dress_html,
        _section("🔔 Other Reminders", _COLORS["reminders_accent"], reminders),
    ])

    if not body.strip():
        body = f"""
        <p style="font-size: 15px; color: {_COLORS['muted']};">
          Nothing new from the school portal or WhatsApp group.
        </p>
        """

    return f"""<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"></head>
  <body style="margin: 0; padding: 24px; background: {_COLORS['page_bg']};
               font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;">
    {banner_html}
    <div style="max-width: 600px; margin: 0 auto; background: {_COLORS['card_bg']};
                border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
      <div style="background: {_COLORS['header_bg']}; color: {_COLORS['header_text']};
                  padding: 20px 24px;">
        <h1 style="margin: 0; font-size: 20px;">🎒 Daily School Brief</h1>
        <p style="margin: 4px 0 0 0; font-size: 13px; opacity: 0.85;">{escape(date)}</p>
      </div>
      <div style="padding: 24px;">
        {body}
      </div>
      <div style="padding: 16px 24px; background: {_COLORS['page_bg']};
                  font-size: 12px; color: {_COLORS['muted']}; text-align: center;">
        Generated automatically from the UDT eschool portal and WhatsApp group.
      </div>
    </div>
  </body>
</html>"""
```

- [ ] **Step 4: Run the tests, confirm they pass**

```bash
python -m pytest pi_service/tests/test_render_brief.py -v
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add pi_service/render_brief.py pi_service/tests/test_render_brief.py
git commit -m "pi_service: add render_brief.py (adapted from render_email.py, adds banner_html)"
```

---

### Task 7: `generate_brief.py`

**Files:**
- Create: `pi_service/generate_brief.py`
- Create: `pi_service/tests/test_generate_brief.py`

- [ ] **Step 1: Write tests for the pure/non-API-calling pieces first**

```python
import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pi_service.generate_brief as generate_brief

IST = ZoneInfo("Asia/Kolkata")


def _envelope(portal_messages=None, whatsapp_messages=None, portal_error=None, whatsapp_error=None):
    return {
        "portal": {"messages": portal_messages or [], "error": portal_error},
        "whatsapp": {"messages": whatsapp_messages or [], "error": whatsapp_error},
    }


def test_generate_brief_short_circuits_when_nothing_to_report():
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)  # Monday
    result = generate_brief.generate_brief(_envelope(), now=now)
    assert result == {
        "date": "Tuesday, September 08, 2026",
        "warnings": [],
        "aviraj_highlight": None,
        "classwork": [],
        "homework": [],
        "agenda": [],
        "dress_code": None,
        "reminders": [],
    }


def test_generate_brief_includes_warnings_for_source_errors():
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)
    result = generate_brief.generate_brief(
        _envelope(portal_error="RuntimeError: login failed"), now=now
    )
    assert result["warnings"] == ["Couldn't reach school portal: RuntimeError: login failed"]


def test_build_prompt_includes_today_and_tomorrow_labels():
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)  # Monday
    prompt = generate_brief.build_prompt(_envelope(portal_messages=[{"id": "1"}]), now)
    assert "Monday, September 07, 2026" in prompt
    assert "Tuesday, September 08, 2026" in prompt


def test_build_prompt_embeds_envelope_messages_as_json():
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)
    envelope = _envelope(portal_messages=[{"id": "1", "title": "Homework"}])
    prompt = generate_brief.build_prompt(envelope, now)
    assert "Homework" in prompt
    assert json.dumps({"portal": envelope["portal"]["messages"], "whatsapp": []}, indent=2) in prompt


def test_collect_attachment_blocks_skips_missing_files(tmp_path):
    envelope = _envelope(
        portal_messages=[{"id": "1", "attachments": [{"name": "x", "saved_as": str(tmp_path / "missing.pdf")}]}]
    )
    assert generate_brief._collect_attachment_blocks(envelope) == []


def test_collect_attachment_blocks_encodes_existing_pdf(tmp_path):
    pdf_path = tmp_path / "homework.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf bytes")
    envelope = _envelope(
        whatsapp_messages=[{"timestamp": "x", "attachments": [{"name": "homework.pdf", "saved_as": str(pdf_path)}]}]
    )
    blocks = generate_brief._collect_attachment_blocks(envelope)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    decoded = base64.standard_b64decode(blocks[0]["source"]["data"])
    assert decoded == b"%PDF-1.4 fake pdf bytes"
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
python -m pytest pi_service/tests/test_generate_brief.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'pi_service.generate_brief'`.

- [ ] **Step 3: Write `pi_service/generate_brief.py`**

```python
"""
Calls the Anthropic API directly to categorize the daily school brief
from the current portal + WhatsApp envelope, replacing the daily-school-
brief Claude Code skill's judgment step (see
.claude/skills/daily-school-brief/SKILL.md steps 4-8, which
CATEGORIZATION_RULES below reproduces) for pi_service's unattended,
non-interactive context. PDF attachments are sent as document content
blocks instead of being Read directly, and "today"/"tomorrow" are
computed here (IST) rather than relied on from the model's own sense of
the current date - the API has no equivalent of Claude Code's own
"today's date is..." environment context.

`date` and `warnings` in the returned dict are always overwritten with
code-computed values after the API call, never left to the model - the
model has no reliable way to know the real wall-clock date, and fetch
errors are known facts from the caller, not something to infer from the
message text.
"""

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
MODEL = "claude-opus-5"


class DailyBrief(BaseModel):
    date: str
    warnings: list[str]
    aviraj_highlight: str | None
    classwork: list[str]
    homework: list[str]
    agenda: list[str]
    dress_code: str | None
    reminders: list[str]


CATEGORIZATION_RULES = """\
You are generating a daily brief for a parent, combining messages from a school
parent portal and a school WhatsApp group. The child this brief is for is
Aviraj.

Today's date (IST, India Standard Time - the school's timezone) is
{today_label} ({today_weekday}). Tomorrow is {tomorrow_label}. Use these
dates for every date-relative rule below - do not use any other notion of
"today" or "tomorrow".

You are given the full set of portal and WhatsApp messages as JSON below,
plus any PDF attachments referenced by those messages (a monthly planner,
an assembly schedule, etc.) as separate document attachments in this same
request - homework/agenda/dress-code details are often inside the document
itself, not the message body text, so read every attached PDF, not just
the message text.

Extract the following, using your own judgment on unstructured free text -
do not pattern-match on fixed keywords:

- Aviraj-specific highlight (`aviraj_highlight`): whenever a message names
  Aviraj specifically (rather than just addressing the class in general),
  call that out - a task or note addressed to him by name is more
  important than a generic class-wide notice. If no message names him
  specifically, leave this null.

- Per-student roster check: some documents (e.g. an assembly schedule)
  pair each day's specific content - a "thought of the day," a "word of
  the day," a specific activity - with the named student assigned to
  present or lead it that day. When that pairing exists, only include the
  content if Aviraj is the student named for the relevant day - check the
  roster/assignment table before including anything from it. If he isn't
  assigned anything for that day, leave that entire item out entirely -
  don't report a classmate's assigned thought/word/activity as if it were
  general class information. A genuinely class-wide detail with no
  per-student assignment attached (e.g. "no school Friday for a holiday")
  is unaffected by this and should still be included normally.

- `classwork`: what was covered in class (typically marked "C.W." in a
  Flow of the Day post) on the most recent school day.

- `homework`: any assignment, reading, or task mentioned for the child to
  do (typically marked "Recap" in a Flow of the Day post).

  Carry-forward rule: `classwork` and `homework` should always reflect the
  most recent school day that had a Flow of the Day post, even if that's
  not today. If today is a weekend or a holiday (so no new Flow of the Day
  post exists for today), carry forward the most recent available post's
  classwork and homework rather than leaving these empty - don't add a
  warning or note explaining why (a holiday/weekend is expected and
  ordinary, not an anomaly worth flagging).

- `agenda`: events, special activities, holidays, timing changes, or
  notices that apply to tomorrow specifically. Content posted at any point
  in the past (e.g. a monthly planner from weeks ago) that happens to
  apply to tomorrow counts just as much as something posted today - you
  are given the full message archive, not just recent messages, so don't
  assume only recent messages matter.

- `dress_code`: any uniform/dress instructions that apply tomorrow (e.g.
  "sports day, wear house colors", "PE kit tomorrow"). Also apply this
  fixed weekly schedule regardless of what the messages say (it isn't
  sourced from the portal/WhatsApp - it's a standing fact about this
  child's week): swimming is every Tuesday and skating is every Thursday.
  If today is Monday, include a reminder to pack swimming dress for
  tomorrow. If today is Wednesday, include a reminder to pack skating
  shoes for tomorrow.

  Override rule: if a message calls out a specific dress code for
  tomorrow (a special event, a change of plan, "no swimming/skating
  tomorrow", etc.), that message-sourced instruction wins - state that one
  instead of the fixed swim/skate reminder above, don't report both. Only
  fall back to the fixed weekly swim/skate reminder when nothing in the
  messages says otherwise for that specific day. If neither applies, leave
  this null.

- `reminders`: anything else worth a parent's attention (fee due dates,
  forms to sign, items to bring, competitions, events) that doesn't fit
  the above, but only if its date falls within the next 5 days from today
  (inclusive of tomorrow). If a message mentions something further out
  than that, don't surface it yet - it'll naturally get picked up on a
  later day's run once it's within the 5-day window.

Leave a field empty (empty list, or null for `aviraj_highlight`/
`dress_code`) rather than forcing content into it when there's nothing
relevant for that field. Set `warnings` to an empty list - it is
overwritten by the caller with accurate fetch-error information after
this call, so leave it as `[]` regardless of what you observe in the
message content. Set `date` to "{tomorrow_label}" - it is also overwritten
by the caller, so its exact value here doesn't matter.

Portal and WhatsApp messages (JSON):
{envelope_json}
"""


def _collect_attachment_blocks(envelope: dict) -> list[dict]:
    blocks = []
    for source in ("portal", "whatsapp"):
        for message in envelope[source]["messages"]:
            for att in message.get("attachments", []):
                saved_as = att.get("saved_as")
                if not saved_as or not Path(saved_as).exists():
                    continue
                data = base64.standard_b64encode(Path(saved_as).read_bytes()).decode("ascii")
                blocks.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                })
    return blocks


def build_prompt(envelope: dict, now: datetime) -> str:
    tomorrow = now + timedelta(days=1)
    envelope_for_prompt = {
        "portal": envelope["portal"]["messages"],
        "whatsapp": envelope["whatsapp"]["messages"],
    }
    return CATEGORIZATION_RULES.format(
        today_label=now.strftime("%A, %B %d, %Y"),
        today_weekday=now.strftime("%A"),
        tomorrow_label=tomorrow.strftime("%A, %B %d, %Y"),
        envelope_json=json.dumps(envelope_for_prompt, indent=2, ensure_ascii=False),
    )


def generate_brief(envelope: dict, now: datetime | None = None) -> dict:
    if now is None:
        now = datetime.now(IST)
    tomorrow = now + timedelta(days=1)
    date_label = tomorrow.strftime("%A, %B %d, %Y")

    warnings = []
    portal_error = envelope["portal"]["error"]
    whatsapp_error = envelope["whatsapp"]["error"]
    if portal_error:
        warnings.append(f"Couldn't reach school portal: {portal_error}")
    if whatsapp_error:
        warnings.append(f"Couldn't reach WhatsApp group: {whatsapp_error}")

    if not envelope["portal"]["messages"] and not envelope["whatsapp"]["messages"]:
        return {
            "date": date_label,
            "warnings": warnings,
            "aviraj_highlight": None,
            "classwork": [],
            "homework": [],
            "agenda": [],
            "dress_code": None,
            "reminders": [],
        }

    client = anthropic.Anthropic()
    prompt = build_prompt(envelope, now)
    content = _collect_attachment_blocks(envelope) + [{"type": "text", "text": prompt}]
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
        output_format=DailyBrief,
    )
    data = response.parsed_output.model_dump()
    data["date"] = date_label
    data["warnings"] = warnings
    return data
```

- [ ] **Step 4: Run the tests, confirm they pass**

```bash
python -m pytest pi_service/tests/test_generate_brief.py -v
```
Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pi_service/generate_brief.py pi_service/tests/test_generate_brief.py
git commit -m "pi_service: add generate_brief.py's prompt building and attachment collection"
```

- [ ] **Step 6: Write a test for the Anthropic API call itself, mocked**

Add to `pi_service/tests/test_generate_brief.py`:

```python
def test_generate_brief_calls_anthropic_and_forces_date_and_warnings(monkeypatch):
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)
    envelope = _envelope(portal_messages=[{"id": "1", "title": "Flow of the Day", "attachments": []}])
    captured = {}

    class FakeParsedOutput:
        def model_dump(self):
            return {
                "date": "wrong-date-the-model-should-not-control-this",
                "warnings": ["model should not control this either"],
                "aviraj_highlight": "Aviraj is presenting tomorrow",
                "classwork": ["Chapter 3"],
                "homework": ["Worksheet 4"],
                "agenda": [],
                "dress_code": None,
                "reminders": [],
            }

    class FakeResponse:
        parsed_output = FakeParsedOutput()

    class FakeMessages:
        def parse(self, **kwargs):
            captured["kwargs"] = kwargs
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(generate_brief.anthropic, "Anthropic", lambda: FakeClient())

    result = generate_brief.generate_brief(envelope, now=now)

    assert result["date"] == "Tuesday, September 08, 2026"
    assert result["warnings"] == []
    assert result["homework"] == ["Worksheet 4"]
    assert result["aviraj_highlight"] == "Aviraj is presenting tomorrow"
    assert captured["kwargs"]["output_format"] is generate_brief.DailyBrief
    assert captured["kwargs"]["model"] == "claude-opus-5"
```

- [ ] **Step 7: Run the full test file, confirm it passes**

```bash
python -m pytest pi_service/tests/test_generate_brief.py -v
```
Expected: all 7 tests pass.

- [ ] **Step 8: Commit**

```bash
git add pi_service/tests/test_generate_brief.py
git commit -m "pi_service: test generate_brief.py's Anthropic call is mocked and date/warnings are forced"
```

---

### Task 8: `service.py` — pollers

**Files:**
- Create: `pi_service/service.py`
- Create: `pi_service/tests/test_service.py`

- [ ] **Step 1: Write tests for `PortalPoller` and `WhatsAppPoller`**

```python
import pi_service.service as service


def test_portal_poller_reports_changed_when_new_messages_found(monkeypatch):
    poller = service.PortalPoller()
    monkeypatch.setattr(service.portal_archive, "compute_portal_cutoff", lambda: "cutoff")
    monkeypatch.setattr(service.scrape_udt, "fetch_recent_messages", lambda **kwargs: [{"id": "1"}])
    monkeypatch.setattr(service.portal_archive, "merge_into_archive", lambda new: [{"id": "1"}])
    monkeypatch.setattr(service.portal_archive, "prune_archive", lambda archive: archive)
    monkeypatch.setattr(service.portal_archive, "save_archive", lambda archive: None)
    monkeypatch.setattr(service.portal_archive, "save_last_run", lambda dt: None)

    assert poller.tick() is True
    assert poller.last_error is None


def test_portal_poller_reports_unchanged_when_no_new_messages(monkeypatch):
    poller = service.PortalPoller()
    monkeypatch.setattr(service.portal_archive, "compute_portal_cutoff", lambda: "cutoff")
    monkeypatch.setattr(service.scrape_udt, "fetch_recent_messages", lambda **kwargs: [])
    monkeypatch.setattr(service.portal_archive, "merge_into_archive", lambda new: [])
    monkeypatch.setattr(service.portal_archive, "prune_archive", lambda archive: archive)
    monkeypatch.setattr(service.portal_archive, "save_archive", lambda archive: None)
    monkeypatch.setattr(service.portal_archive, "save_last_run", lambda dt: None)

    assert poller.tick() is False


def test_portal_poller_records_error_and_does_not_raise(monkeypatch):
    poller = service.PortalPoller()
    monkeypatch.setattr(service.portal_archive, "compute_portal_cutoff", lambda: "cutoff")

    def failing_fetch(**kwargs):
        raise RuntimeError("login failed")

    monkeypatch.setattr(service.scrape_udt, "fetch_recent_messages", failing_fetch)

    assert poller.tick() is False
    assert poller.last_error == "RuntimeError: login failed"


def test_whatsapp_poller_first_tick_never_reports_changed(monkeypatch):
    poller = service.WhatsAppPoller()
    monkeypatch.setattr(service.fetch_whatsapp, "fetch_recent_whatsapp_messages", lambda **kwargs: [{"sender": "A"}])

    assert poller.tick() is False
    assert poller.last_messages == [{"sender": "A"}]


def test_whatsapp_poller_reports_changed_when_messages_differ(monkeypatch):
    poller = service.WhatsAppPoller()
    responses = iter([[{"sender": "A"}], [{"sender": "A"}, {"sender": "B"}]])
    monkeypatch.setattr(service.fetch_whatsapp, "fetch_recent_whatsapp_messages", lambda **kwargs: next(responses))

    assert poller.tick() is False
    assert poller.tick() is True


def test_whatsapp_poller_reports_unchanged_when_messages_are_the_same(monkeypatch):
    poller = service.WhatsAppPoller()
    monkeypatch.setattr(service.fetch_whatsapp, "fetch_recent_whatsapp_messages", lambda **kwargs: [{"sender": "A"}])

    poller.tick()
    assert poller.tick() is False


def test_whatsapp_poller_records_error_and_keeps_last_good_messages(monkeypatch):
    poller = service.WhatsAppPoller()
    monkeypatch.setattr(service.fetch_whatsapp, "fetch_recent_whatsapp_messages", lambda **kwargs: [{"sender": "A"}])
    poller.tick()

    def failing_fetch(**kwargs):
        raise RuntimeError("imap down")

    monkeypatch.setattr(service.fetch_whatsapp, "fetch_recent_whatsapp_messages", failing_fetch)

    assert poller.tick() is False
    assert poller.last_error == "RuntimeError: imap down"
    assert poller.last_messages == [{"sender": "A"}]
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
python -m pytest pi_service/tests/test_service.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'pi_service.service'`.

- [ ] **Step 3: Write `pi_service/service.py` (pollers only so far)**

```python
"""
Always-on entrypoint for the daily school brief pi_service: polls the
school portal (60 min) and WhatsApp (10 min) for new content, forces one
daily regeneration as a backstop (~7:30pm IST) so date-relative fields
stay correct even without new messages, and serves the latest brief on a
LAN-only web page. Run via `python -m pi_service.service`.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask

from pi_service import fetch_whatsapp, generate_brief, portal_archive, render_brief, scrape_udt
from pi_service.cutoff import month_anchor

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
WHATSAPP_POLL_SECONDS = 600
PORTAL_POLL_SECONDS = 3600
PORT = int(os.getenv("PORT", "8765"))

OUTPUT_DIR = Path(__file__).parent / "output"
CONTENT_PATH = OUTPUT_DIR / "daily_brief_content.json"
LAST_ATTEMPT_PATH = OUTPUT_DIR / "last_attempt.json"

app = Flask(__name__)


class PortalPoller:
    """Fetches new portal messages since the cursor and merges them into
    the archive. `tick()` returns True if anything new was fetched (the
    brief itself is always built from the FULL archive - see
    regenerate_brief - not from this method's return value)."""

    def __init__(self) -> None:
        self.last_error: str | None = None

    def tick(self) -> bool:
        try:
            cutoff = portal_archive.compute_portal_cutoff()
            new_messages = scrape_udt.fetch_recent_messages(cutoff=cutoff, download_attachments=True)
            merged = portal_archive.merge_into_archive(new_messages)
            pruned = portal_archive.prune_archive(merged)
            portal_archive.save_archive(pruned)
            portal_archive.save_last_run(datetime.now())
            self.last_error = None
            return bool(new_messages)
        except Exception as e:  # noqa: BLE001 - best-effort per source, matches daily_brief.py's contract
            self.last_error = f"{type(e).__name__}: {e}"
            return False


class WhatsAppPoller:
    """Re-fetches the full WhatsApp window every tick (cheap IMAP call)
    and detects new content by diffing against the previous tick's
    result, rather than a persisted cursor - see the design doc for why a
    cursor would silently drop older-but-still-relevant messages from the
    brief."""

    def __init__(self) -> None:
        self.last_messages: list[dict] | None = None
        self.last_error: str | None = None
        self._previous_messages: list[dict] | None = None

    def tick(self) -> bool:
        try:
            messages = fetch_whatsapp.fetch_recent_whatsapp_messages(cutoff=month_anchor(months_back=3))
        except Exception as e:  # noqa: BLE001 - best-effort per source, matches daily_brief.py's contract
            self.last_error = f"{type(e).__name__}: {e}"
            return False
        self.last_error = None
        self.last_messages = messages
        previous = self._previous_messages
        self._previous_messages = messages
        if previous is None:
            return False
        return messages != previous
```

- [ ] **Step 4: Run the tests, confirm they pass**

```bash
python -m pytest pi_service/tests/test_service.py -v
```
Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pi_service/service.py pi_service/tests/test_service.py
git commit -m "pi_service: add service.py's PortalPoller and WhatsAppPoller"
```

---

### Task 9: `service.py` — daily backstop timing and `regenerate_brief`

**Files:**
- Modify: `pi_service/service.py`
- Modify: `pi_service/tests/test_service.py`

- [ ] **Step 1: Add tests for `next_daily_backstop` and `regenerate_brief`**

Add to `pi_service/tests/test_service.py`:

```python
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def test_next_daily_backstop_returns_today_when_before_time():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=IST)
    assert service.next_daily_backstop(now) == datetime(2026, 9, 7, 19, 30, tzinfo=IST)


def test_next_daily_backstop_returns_tomorrow_when_after_time():
    now = datetime(2026, 9, 7, 20, 0, tzinfo=IST)
    assert service.next_daily_backstop(now) == datetime(2026, 9, 8, 19, 30, tzinfo=IST)


def test_next_daily_backstop_returns_tomorrow_when_exactly_at_time():
    now = datetime(2026, 9, 7, 19, 30, tzinfo=IST)
    assert service.next_daily_backstop(now) == datetime(2026, 9, 8, 19, 30, tzinfo=IST)


def test_regenerate_brief_writes_content(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "CONTENT_PATH", tmp_path / "daily_brief_content.json")
    monkeypatch.setattr(service, "LAST_ATTEMPT_PATH", tmp_path / "last_attempt.json")
    monkeypatch.setattr(service.portal_archive, "load_archive", lambda: [{"id": "1"}])
    monkeypatch.setattr(
        service.generate_brief, "generate_brief",
        lambda envelope: {"date": "d", "warnings": [], "aviraj_highlight": None,
                           "classwork": [], "homework": ["hw"], "agenda": [],
                           "dress_code": None, "reminders": []},
    )

    portal_poller = service.PortalPoller()
    whatsapp_poller = service.WhatsAppPoller()
    whatsapp_poller.last_messages = [{"sender": "A"}]

    service.regenerate_brief(portal_poller, whatsapp_poller, threading.Lock())

    content = json.loads((tmp_path / "daily_brief_content.json").read_text())
    assert content["homework"] == ["hw"]
    attempt = json.loads((tmp_path / "last_attempt.json").read_text())
    assert attempt["succeeded"] is True


def test_regenerate_brief_records_failure_without_touching_existing_content(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    content_path = tmp_path / "daily_brief_content.json"
    content_path.write_text(json.dumps({"date": "yesterday"}))
    monkeypatch.setattr(service, "CONTENT_PATH", content_path)
    monkeypatch.setattr(service, "LAST_ATTEMPT_PATH", tmp_path / "last_attempt.json")
    monkeypatch.setattr(service.portal_archive, "load_archive", lambda: [])

    def failing_generate(envelope):
        raise RuntimeError("api down")

    monkeypatch.setattr(service.generate_brief, "generate_brief", failing_generate)

    portal_poller = service.PortalPoller()
    whatsapp_poller = service.WhatsAppPoller()

    service.regenerate_brief(portal_poller, whatsapp_poller, threading.Lock())

    assert json.loads(content_path.read_text()) == {"date": "yesterday"}
    attempt = json.loads((tmp_path / "last_attempt.json").read_text())
    assert attempt["succeeded"] is False
    assert "api down" in attempt["error"]
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
python -m pytest pi_service/tests/test_service.py -v
```
Expected: FAIL with `AttributeError: module 'pi_service.service' has no attribute 'next_daily_backstop'`.

- [ ] **Step 3: Add `next_daily_backstop`, `_write_last_attempt`, and `regenerate_brief` to `pi_service/service.py`**

Append after the `WhatsAppPoller` class:

```python
def next_daily_backstop(now: datetime, hour: int = 19, minute: int = 30) -> datetime:
    """Next occurrence of `hour:minute` at or after `now`, in `now`'s own
    timezone. Returns tomorrow's occurrence if `now` is already at or past
    today's."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _write_last_attempt(succeeded: bool, error: str | None) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    LAST_ATTEMPT_PATH.write_text(json.dumps(
        {"succeeded": succeeded, "at": datetime.now(IST).isoformat(), "error": error},
        indent=2,
    ))


def regenerate_brief(portal_poller: PortalPoller, whatsapp_poller: WhatsAppPoller, lock: threading.Lock) -> None:
    """Builds the current envelope from each poller's latest known state,
    calls the LLM, and writes the content JSON - or, on failure, leaves
    the last successfully-generated content untouched and records the
    failure for the web page (which re-renders HTML from this JSON on
    every request) to surface instead."""
    with lock:
        envelope = {
            "portal": {"messages": portal_archive.load_archive(), "error": portal_poller.last_error},
            "whatsapp": {"messages": whatsapp_poller.last_messages or [], "error": whatsapp_poller.last_error},
        }
        try:
            data = generate_brief.generate_brief(envelope)
            OUTPUT_DIR.mkdir(exist_ok=True)
            CONTENT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            _write_last_attempt(succeeded=True, error=None)
        except Exception as e:  # noqa: BLE001 - keep serving the last good brief rather than crashing the service
            _write_last_attempt(succeeded=False, error=f"{type(e).__name__}: {e}")
```

- [ ] **Step 4: Run the tests, confirm they pass**

```bash
python -m pytest pi_service/tests/test_service.py -v
```
Expected: all 12 tests pass (7 from Task 8 + 3 backstop-timing + 2 regenerate_brief).

- [ ] **Step 5: Commit**

```bash
git add pi_service/service.py pi_service/tests/test_service.py
git commit -m "pi_service: add next_daily_backstop and regenerate_brief"
```

---

### Task 10: `service.py` — Flask web page

**Files:**
- Modify: `pi_service/service.py`
- Modify: `pi_service/tests/test_service.py`

- [ ] **Step 1: Add tests for the `/` route**

Add to `pi_service/tests/test_service.py`:

```python
def test_index_shows_placeholder_when_no_brief_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CONTENT_PATH", tmp_path / "daily_brief_content.json")
    monkeypatch.setattr(service, "LAST_ATTEMPT_PATH", tmp_path / "last_attempt.json")
    client = service.app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    assert b"No brief generated yet" in response.data


def test_index_renders_latest_brief(tmp_path, monkeypatch):
    content_path = tmp_path / "daily_brief_content.json"
    content_path.write_text(json.dumps({
        "date": "Tuesday, September 08, 2026", "warnings": [], "aviraj_highlight": None,
        "classwork": [], "homework": ["Read pages 10-12"], "agenda": [],
        "dress_code": None, "reminders": [],
    }))
    monkeypatch.setattr(service, "CONTENT_PATH", content_path)
    monkeypatch.setattr(service, "LAST_ATTEMPT_PATH", tmp_path / "last_attempt.json")
    client = service.app.test_client()

    response = client.get("/")

    assert b"Read pages 10-12" in response.data


def test_index_shows_failure_banner_when_last_attempt_failed(tmp_path, monkeypatch):
    content_path = tmp_path / "daily_brief_content.json"
    content_path.write_text(json.dumps({
        "date": "d", "warnings": [], "aviraj_highlight": None, "classwork": [],
        "homework": [], "agenda": [], "dress_code": None, "reminders": [],
    }))
    last_attempt_path = tmp_path / "last_attempt.json"
    last_attempt_path.write_text(json.dumps({
        "succeeded": False, "at": "2026-09-07T19:30:00+05:30", "error": "api down",
    }))
    monkeypatch.setattr(service, "CONTENT_PATH", content_path)
    monkeypatch.setattr(service, "LAST_ATTEMPT_PATH", last_attempt_path)
    client = service.app.test_client()

    response = client.get("/")

    assert b"api down" in response.data
```

- [ ] **Step 2: Run the tests, confirm they fail**

```bash
python -m pytest pi_service/tests/test_service.py -v -k index
```
Expected: FAIL — no `/` route registered yet (404s, or the assertions on response content fail).

- [ ] **Step 3: Add the `/` route to `pi_service/service.py`**

Append after `regenerate_brief`:

```python
@app.route("/")
def index() -> str:
    if not CONTENT_PATH.exists():
        return "<h1>Daily School Brief</h1><p>No brief generated yet.</p>"

    data = json.loads(CONTENT_PATH.read_text())
    banner_html = ""
    if LAST_ATTEMPT_PATH.exists():
        attempt = json.loads(LAST_ATTEMPT_PATH.read_text())
        if not attempt["succeeded"]:
            banner_html = (
                '<div style="max-width: 600px; margin: 0 auto 16px auto; padding: 12px 16px; '
                'background: #FEE2E2; border-left: 4px solid #DC2626; border-radius: 4px; '
                'font-size: 14px;">'
                f'Last regeneration attempt at {attempt["at"]} failed: {attempt["error"]}. '
                "Showing the last successful brief below."
                "</div>"
            )
    return render_brief.render_brief_html(data, banner_html=banner_html)
```

- [ ] **Step 4: Run the tests, confirm they pass**

```bash
python -m pytest pi_service/tests/test_service.py -v
```
Expected: all 15 tests pass (12 from Task 9 + 3 index-route tests).

- [ ] **Step 5: Commit**

```bash
git add pi_service/service.py pi_service/tests/test_service.py
git commit -m "pi_service: add Flask index route serving the latest brief"
```

---

### Task 11: `service.py` — wire up `main()`

**Files:**
- Modify: `pi_service/service.py`

This wires the pollers, backstop timing, and web server into the running
service. It isn't unit-tested (an infinite loop across threads isn't a
meaningful unit boundary) — Task 13 verifies it end-to-end instead.

- [ ] **Step 1: Add `main()` to the end of `pi_service/service.py`**

```python
def main() -> None:
    lock = threading.Lock()
    portal_poller = PortalPoller()
    whatsapp_poller = WhatsAppPoller()

    def whatsapp_loop() -> None:
        while True:
            if whatsapp_poller.tick():
                regenerate_brief(portal_poller, whatsapp_poller, lock)
            time.sleep(WHATSAPP_POLL_SECONDS)

    def portal_loop() -> None:
        while True:
            if portal_poller.tick():
                regenerate_brief(portal_poller, whatsapp_poller, lock)
            time.sleep(PORTAL_POLL_SECONDS)

    def backstop_loop() -> None:
        while True:
            now = datetime.now(IST)
            next_run = next_daily_backstop(now)
            time.sleep((next_run - now).total_seconds())
            regenerate_brief(portal_poller, whatsapp_poller, lock)

    for target in (whatsapp_loop, portal_loop, backstop_loop):
        threading.Thread(target=target, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the full `pi_service` test suite one more time**

```bash
python -m pytest pi_service/ -v
```
Expected: all tests across every `pi_service/tests/*.py` file pass (cutoff, scrape_udt, portal_archive, fetch_whatsapp, render_brief, generate_brief, service — roughly 75+ tests total).

- [ ] **Step 3: Commit**

```bash
git add pi_service/service.py
git commit -m "pi_service: wire pollers, backstop, and Flask app into main()"
```

---

### Task 12: Deploy to the Raspberry Pi (manual)

Not automatable from this environment — run these steps directly on the
Pi once the code above is pushed/copied there.

- [ ] **Step 1: Get the code onto the Pi** (clone the repo, or `git pull` if already cloned)

- [ ] **Step 2: Set up a dedicated virtualenv for `pi_service`**

```bash
cd /path/to/daily-school-brief
python3 -m venv pi_service/.venv
pi_service/.venv/bin/pip install -r pi_service/requirements.txt
```

- [ ] **Step 3: Configure credentials**

```bash
cp pi_service/.env.example pi_service/.env
# edit pi_service/.env: UDT_USERNAME, UDT_PASSWORD, SMTP_USERNAME,
# SMTP_APP_PASSWORD, WHATSAPP_EMAIL_TO, ANTHROPIC_API_KEY
```

- [ ] **Step 4: Create the systemd unit** at `/etc/systemd/system/daily-school-brief-pi-service.service` (adjust the two absolute paths to match where the repo actually lives on the Pi, e.g. `/home/pi/daily-school-brief`):

```ini
[Unit]
Description=Daily School Brief - always-on Pi service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/daily-school-brief
EnvironmentFile=/home/pi/daily-school-brief/pi_service/.env
ExecStart=/home/pi/daily-school-brief/pi_service/.venv/bin/python -m pi_service.service
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 5: Enable and start it**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now daily-school-brief-pi-service.service
sudo systemctl status daily-school-brief-pi-service.service
```
Expected: `active (running)`.

- [ ] **Step 6: Confirm it survives a reboot**

```bash
sudo reboot
# after it comes back up:
sudo systemctl status daily-school-brief-pi-service.service
```
Expected: `active (running)` again, with no manual restart needed.

---

### Task 13: End-to-end dry run (manual)

- [ ] **Step 1: Watch the logs for the first real poll cycle**

```bash
journalctl -u daily-school-brief-pi-service.service -f
```
Expected: no unhandled exceptions/tracebacks; any fetch errors show up as
caught, logged failures, not a crashed process.

- [ ] **Step 2: Open the web page from another device on the same home Wi-Fi**

Visit `http://<pi-hostname>.local:8765/` (or `http://<pi-lan-ip>:8765/`).
Expected: either "No brief generated yet" (before the first successful
regeneration) or a rendered brief once one has run.

- [ ] **Step 3: Force a real regeneration and check it by hand**

Since the daily backstop may be hours away, temporarily confirm the
pipeline works by checking what a fresh WhatsApp/portal message triggers:
send a test message into the WhatsApp group (or wait for a real portal
post), wait up to one poll interval (≤10 min for WhatsApp, ≤60 min for
portal), then reload the web page.
Expected: the page updates to reflect the new content within one poll
interval, and the categorization looks right by hand (homework, agenda,
dress code, reminders match what you'd expect from the actual message).

- [ ] **Step 4: Confirm the failure path**

Temporarily set an invalid `ANTHROPIC_API_KEY` in `pi_service/.env`,
restart the service, and force a regeneration (as in Step 3).
Expected: the web page keeps showing the last good brief, with a red
"last regeneration attempt ... failed" banner above it — not a blank page
or a 500 error. Restore the real API key and restart the service
afterward.
