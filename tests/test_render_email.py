from render_email import render_brief_html


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


def test_render_includes_clickable_homework_pages_and_escapes_links():
    html = render_brief_html(_base_data(homework_pages=[{
        "name": "Hindi <worksheet>",
        "url": "https://school.example/page.pdf?subject=hindi&day=7",
    }, {"name": "Math workbook", "url": "/pdfs/Math%20workbook.pdf"}]))
    assert 'href="https://school.example/page.pdf?subject=hindi&amp;day=7"' in html
    assert "Hindi &lt;worksheet&gt;" in html
    assert 'href="/pdfs/Math%20workbook.pdf"' in html
    assert "Nothing new" not in html


def test_render_rejects_unsafe_homework_page_urls():
    html = render_brief_html(_base_data(homework_pages=[
        {"name": "Unsafe", "url": "javascript:alert(1)"},
        {"name": "External relative", "url": "//unknown.example/page.pdf"},
        {"name": "Local secret", "url": "file:///private/page.pdf"},
    ]))
    assert "href=" not in html
    assert "Nothing new" in html


def test_render_includes_separate_holidays_and_escapes_reasons():
    html = render_brief_html(_base_data(holidays=[{
        "date": "2026-10-09", "reason": "Closure <notice> & holiday",
    }]))
    assert "School Holidays" in html
    assert "2026-10-09: Closure &lt;notice&gt; &amp; holiday" in html
    assert "Nothing new" not in html


def test_render_omits_empty_holiday_list():
    assert "School Holidays" not in render_brief_html(_base_data(holidays=[]))


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
