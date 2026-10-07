"""
Sends the daily school brief as an HTML email via Gmail SMTP.

generate_brief.py writes validated Azure-generated brief content to
output/daily_brief_content.json; this
script reads that file, renders it into HTML via render_email.py, and
sends it - keeping "what to say" (Azure GPT's judgment),
"how it looks" (render_email.py), and "how to send it" (this module)
as separate concerns.

SETUP:
    - Create a Gmail App Password: Google Account -> Security -> 2-Step
      Verification -> App passwords (requires 2FA enabled on the account).
    - Set SMTP_USERNAME (the Gmail address) and SMTP_APP_PASSWORD (the
      16-character app password, not your regular Gmail password) in .env.
    - Set EMAIL_TO (the recipient address) in .env.

USAGE:
    python send_email.py             # sends the email
    python send_email.py --preview   # skips SMTP, writes output/daily_brief_preview.html
"""

import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

from render_email import render_brief_html

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

CONTENT_PATH = Path(__file__).parent / "output" / "daily_brief_content.json"
PREVIEW_PATH = Path(__file__).parent / "output" / "daily_brief_preview.html"

def _parse_recipients(value: str) -> list[str]:
    """Split a comma-separated recipient string into individual addresses.

    EMAIL_TO may naturally be set to multiple comma-separated addresses
    (the To: header supports that syntax), but sendmail's envelope
    recipient list needs each address as its own list element - passing
    the whole string as a single element silently delivers to only the
    first address with no error.
    """
    return [addr.strip() for addr in value.split(",") if addr.strip()]


def send_brief_email(subject: str, html_body: str) -> None:
    if not SMTP_USERNAME or not SMTP_APP_PASSWORD or not EMAIL_TO:
        raise RuntimeError(
            "SMTP_USERNAME, SMTP_APP_PASSWORD, and/or EMAIL_TO are not set."
        )
    recipients = _parse_recipients(EMAIL_TO)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(SMTP_USERNAME, SMTP_APP_PASSWORD)
        server.sendmail(SMTP_USERNAME, recipients, msg.as_string())


def run(preview: bool = False) -> None:
    data = json.loads(CONTENT_PATH.read_text())
    html = render_brief_html(data)
    if preview:
        PREVIEW_PATH.write_text(html)
        print(f"Preview written to {PREVIEW_PATH}")
        return
    subject = f"Daily School Brief - {data.get('date', '')}"
    send_brief_email(subject, html)
    print(f"Email sent to {EMAIL_TO}")


if __name__ == "__main__":
    run(preview="--preview" in sys.argv[1:])
