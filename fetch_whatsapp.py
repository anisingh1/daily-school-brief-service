"""Fetch school WhatsApp messages and PDFs through Evolution API."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from fetch_evolution import fetch_recent_evolution_messages

load_dotenv()

ATTACHMENTS_DIR = Path(__file__).parent / "data" / "pdfs"


def fetch_recent_whatsapp_messages(
    lookback_hours: float | None = None, cutoff: datetime | None = None,
) -> list[dict]:
    if os.getenv("WHATSAPP_SOURCE", "evolution").strip().lower() != "evolution":
        raise ValueError("Only Evolution is supported; remove the legacy WHATSAPP_SOURCE setting")
    if cutoff is None:
        hours = float(os.getenv("LOOKBACK_HOURS", "36")) if lookback_hours is None else lookback_hours
        cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    return fetch_recent_evolution_messages(cutoff, ATTACHMENTS_DIR)


if __name__ == "__main__":
    print(json.dumps(fetch_recent_whatsapp_messages(), ensure_ascii=False, indent=2))
