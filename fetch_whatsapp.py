"""
Fetch WhatsApp group messages from email capture or Evolution API.

Set WHATSAPP_SOURCE=evolution for direct ingestion (see docs/EVOLUTION_SETUP.md).
The default email source retains the phone automation workflow below.

Messages are captured by two MacroDroid rules on an Android phone (see
docs/PHONE_SETUP.md):
  - A Notification Received rule (WhatsApp, filtered to the school
    group's chat name) sends a plain email for each text message -
    Subject = [WhatsApp School] sender, Body = message text, Date = when it arrived.
  - A File Added rule (watching WhatsApp's own auto-save "Documents"
    folder) sends an email with the new file attached whenever a
    document arrives in any chat.

Both rules send to the same inbound address - a Gmail "+" alias of the
account already used for outbound mail (see WHATSAPP_EMAIL_TO below) -
so this module can find them with a single IMAP search, without a
separate Google Cloud project or service account. It reuses the same
Gmail App Password already configured for send_email.py.

SETUP:
    - Enable IMAP in the Gmail account's settings (Settings -> Forwarding
      and POP/IMAP -> Enable IMAP).
    - Set WHATSAPP_EMAIL_TO in .env to the "+" alias address the phone
      sends to, e.g. "you+whatsapp@gmail.com" (Gmail delivers "+" alias
      mail to the same inbox, so this module can filter for it via IMAP
      search without any separate account or server-side filter).
        - Both capture rules must prefix the email subject with [WhatsApp School],
            or the value configured in WHATSAPP_SUBJECT_PREFIX. Other emails are ignored.

USAGE:
    python fetch_whatsapp.py
"""

import email
import imaplib
import os
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from dateutil import parser as dateparser
from dotenv import load_dotenv

load_dotenv()

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
WHATSAPP_EMAIL_TO = os.getenv("WHATSAPP_EMAIL_TO")
WHATSAPP_SUBJECT_PREFIX = os.getenv("WHATSAPP_SUBJECT_PREFIX", "[WhatsApp School]").strip()
LOOKBACK_HOURS = float(os.getenv("LOOKBACK_HOURS", "36"))

ATTACHMENTS_DIR = Path(__file__).parent / "data" / "pdfs"


def _is_pdf_part(part: Message) -> bool:
    if part.get_content_type() == "application/pdf":
        return True
    filename = part.get_filename() or ""
    return part.get_content_disposition() == "attachment" and filename.lower().endswith(".pdf")


def _decoded_payload(part: Message) -> bytes | None:
    payload = part.get_payload(decode=True)
    if payload is not None and not isinstance(payload, bytes):
        raise ValueError("Email part has a non-byte decoded payload")
    return payload


def _save_pdf_attachment(part: Message, uid: str, save_dir: Path) -> dict:
    filename = part.get_filename() or f"{uid}.pdf"
    save_dir.mkdir(parents=True, exist_ok=True)
    dest = save_dir / f"whatsapp_{uid}_{filename}"
    if not dest.exists():
        payload = _decoded_payload(part)
        if payload is None:
            raise ValueError("PDF attachment has no decoded payload")
        dest.write_bytes(payload)
    return {"name": filename, "saved_as": str(dest)}


def _email_subject(msg: Message) -> str:
    return str(make_header(decode_header(msg.get("Subject", "") or "")))


def parse_email_message(msg: Message, uid: str, save_dir: Path = ATTACHMENTS_DIR) -> dict:
    """Turn one captured email into a WhatsApp message record.

    A PDF attachment (application/pdf, or an "attachment"-disposition
    part with a .pdf filename) is saved to disk and referenced by
    `saved_as` - the same shape scrape_udt.py uses for portal
    attachments, so the skill's existing "Read any attachment with a
    saved_as path" step applies here too. Non-PDF attachments (e.g.
    images) are intentionally ignored - only their absence is asserted,
    not walked further.
    """
    sender = _email_subject(msg).removeprefix(WHATSAPP_SUBJECT_PREFIX).strip()
    timestamp = parsedate_to_datetime(msg.get("Date", "")).isoformat()
    text_parts = []
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            if _is_pdf_part(part):
                attachments.append(_save_pdf_attachment(part, uid, save_dir))
            elif (
                part.get_content_type() == "text/plain"
                and part.get_content_disposition() != "attachment"
            ):
                payload = _decoded_payload(part)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = _decoded_payload(msg)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text_parts.append(payload.decode(charset, errors="replace"))

    return {
        "timestamp": timestamp,
        "sender": sender,
        "text": "".join(text_parts).strip(),
        "attachments": attachments,
    }


def filter_recent(
    messages: list[dict], lookback_hours: float | None = None, cutoff: datetime | None = None
) -> list[dict]:
    if cutoff is None:
        if lookback_hours is None:
            lookback_hours = LOOKBACK_HOURS
        cutoff = datetime.now().astimezone() - timedelta(hours=lookback_hours)
    elif cutoff.tzinfo is None:
        cutoff = cutoff.astimezone()
    filtered = []
    for m in messages:
        raw_ts = m.get("timestamp")
        if not raw_ts:
            continue
        try:
            ts = dateparser.parse(raw_ts)
        except (ValueError, TypeError, OverflowError):
            continue
        if ts.tzinfo is None:
            ts = ts.astimezone()
        if ts >= cutoff:
            filtered.append(m)
    filtered.sort(key=lambda m: dateparser.parse(m["timestamp"]).astimezone())
    return filtered


def _connect_imap() -> imaplib.IMAP4_SSL:
    if not SMTP_USERNAME or not SMTP_APP_PASSWORD or not WHATSAPP_EMAIL_TO:
        raise RuntimeError(
            "SMTP_USERNAME, SMTP_APP_PASSWORD, and/or WHATSAPP_EMAIL_TO are not set."
        )
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(SMTP_USERNAME, SMTP_APP_PASSWORD)
    conn.select("INBOX")
    return conn


def whatsapp_source() -> str:
    source = os.getenv("WHATSAPP_SOURCE", "email").strip().lower()
    if source not in {"email", "evolution"}:
        raise ValueError("WHATSAPP_SOURCE must be email or evolution")
    return source


def fetch_recent_whatsapp_messages(
    lookback_hours: float | None = None, cutoff: datetime | None = None
) -> list[dict]:
    if whatsapp_source() == "evolution":
        from fetch_evolution import fetch_recent_evolution_messages

        if cutoff is None:
            cutoff = datetime.now().astimezone() - timedelta(
                hours=LOOKBACK_HOURS if lookback_hours is None else lookback_hours,
            )
        return fetch_recent_evolution_messages(cutoff, ATTACHMENTS_DIR)
    return _fetch_recent_email_messages(lookback_hours=lookback_hours, cutoff=cutoff)


def _fetch_recent_email_messages(
    lookback_hours: float | None = None, cutoff: datetime | None = None
) -> list[dict]:
    if not WHATSAPP_SUBJECT_PREFIX:
        raise RuntimeError("WHATSAPP_SUBJECT_PREFIX must not be empty")
    if cutoff is None:
        if lookback_hours is None:
            lookback_hours = LOOKBACK_HOURS
        cutoff = datetime.now().astimezone() - timedelta(hours=lookback_hours)
    since_date = cutoff.strftime("%d-%b-%Y")

    conn = _connect_imap()
    try:
        _, data = conn.search(None, "TO", f'"{WHATSAPP_EMAIL_TO}"', "SINCE", since_date)
        uids = data[0].split() if data and data[0] else []
        messages = []
        for uid in uids:
            fetch_status, msg_data = conn.fetch(uid, "(RFC822)")
            if fetch_status != "OK":
                raise RuntimeError("IMAP message fetch failed")
            raw = next((item[1] for item in msg_data
                        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes)), None)
            if raw is None:
                raise RuntimeError("IMAP response did not contain email bytes")
            msg = email.message_from_bytes(raw)
            subject = _email_subject(msg)
            if subject != WHATSAPP_SUBJECT_PREFIX and not subject.startswith(WHATSAPP_SUBJECT_PREFIX + " "):
                continue
            timestamp = parsedate_to_datetime(msg.get("Date", "")).isoformat()
            if not filter_recent([{"timestamp": timestamp}], cutoff=cutoff):
                continue
            messages.append(parse_email_message(msg, uid.decode()))
    finally:
        conn.logout()

    return filter_recent(messages, cutoff=cutoff)


if __name__ == "__main__":
    for msg in fetch_recent_whatsapp_messages():
        print(msg)
