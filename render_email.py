"""
Renders the daily school brief's structured content into a polished,
section-based HTML email body.

Takes a structured dict (produced and validated by generate_brief.py)
rather than raw scraped messages, so the visual design is deterministic
and consistent every day regardless of that day's content. Azure GPT
decides what goes in the brief; this module decides how it looks.

Expected `data` keys: `date` (str), `warnings` (list[str]),
`aviraj_highlight` (str | None), `classwork` (list[str]), `homework`
(list[str]), `homework_pages` (list[dict[str, str]]), `agenda` (list[str]), `dress_code` (str | None),
`reminders` (list[str]), `holidays` (list[dict[str, str]] with date and reason).
"""

from html import escape
from urllib.parse import urlsplit


def _as_list(value) -> list[str]:
    """Normalize a list-typed field that may arrive as a bare string.

    Legacy or hand-authored content may contain a bare string instead of a
    single-element list. Iterating a bare string directly would silently
    produce one <li> per character, so callers should always route
    list-typed fields through this helper first.
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


def _section(title: str, accent: str, items: list[str],
        links: list[dict[str, str]] | None = None) -> str:
    items = [item for item in items if item and item.strip()]
    rows = "".join(
        f'<li style="margin: 0 0 8px 0; line-height: 1.5;">{escape(item)}</li>'
        for item in items
    )
    for link in links or []:
        url = link.get("url", "")
        name = link.get("name", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        is_web_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        is_pdf_path = not parsed.scheme and not parsed.netloc and parsed.path.startswith("/pdfs/")
        if name.strip() and (is_web_url or is_pdf_path):
            rows += (
                '<li style="margin: 0 0 8px 0; line-height: 1.5;">'
                f'<a href="{escape(url, quote=True)}">{escape(name)}</a></li>'
            )
    if not rows:
        return ""
    return f"""
    <div style="margin: 0 0 24px 0; padding: 16px 20px; background: #FAFAFA;
                border-left: 4px solid {accent}; border-radius: 4px;">
      <h2 style="margin: 0 0 8px 0; font-size: 16px; color: {_COLORS['text']};">
        {escape(title)}
      </h2>
      <ul style="margin: 0; padding-left: 20px;">{rows}</ul>
    </div>
    """


def render_brief_html(data: dict) -> str:
    date = data.get("date", "")
    warnings = [w for w in _as_list(data.get("warnings")) if w and w.strip()]
    aviraj_highlight = data.get("aviraj_highlight")
    aviraj_highlight = (
        aviraj_highlight if aviraj_highlight and aviraj_highlight.strip() else None
    )
    classwork = _as_list(data.get("classwork"))
    homework = _as_list(data.get("homework"))
    agenda = _as_list(data.get("agenda"))
    holidays = [f"{holiday['date']}: {holiday['reason']}" for holiday in data.get("holidays", [])
                if holiday.get("date") and holiday.get("reason")]
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
        _section("📚 Homework", _COLORS["homework_accent"], homework, data.get("homework_pages")),
        _section("📅 Tomorrow's Agenda", _COLORS["agenda_accent"], agenda),
        _section("School Holidays", _COLORS["agenda_accent"], holidays),
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
