"""Store portal and WhatsApp messages for the configured retention window.

Source cursors limit fetching to new messages within the retention window.
Cleanup uses message timestamps in IST for
archives and referenced PDFs, and modification times for orphaned files.
Run this module directly for cleanup without fetching or generating a brief.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DATA_DIR = Path(__file__).parent / "data"
ARCHIVE_PATH = DATA_DIR / "portal_messages.json"
LAST_RUN_PATH = DATA_DIR / "last_run.json"
WHATSAPP_ARCHIVE_PATH = DATA_DIR / "whatsapp_messages.json"
OUTPUT_DIR = Path(__file__).parent / "output"


def read_last_run() -> datetime | None:
    if not LAST_RUN_PATH.exists():
        return None
    data = json.loads(LAST_RUN_PATH.read_text())
    raw = data.get("last_run")
    if not raw:
        return None
    return dateparser.parse(raw)


def save_last_run(dt: datetime) -> None:
    data = json.loads(LAST_RUN_PATH.read_text()) if LAST_RUN_PATH.exists() else {}
    data["last_run"] = dt.isoformat()
    write_json(LAST_RUN_PATH, data)


def read_whatsapp_last_run() -> datetime | None:
    if not LAST_RUN_PATH.exists():
        return None
    raw = json.loads(LAST_RUN_PATH.read_text()).get("whatsapp_last_run")
    return dateparser.parse(raw) if raw else None


def save_whatsapp_last_run(dt: datetime) -> None:
    data = json.loads(LAST_RUN_PATH.read_text()) if LAST_RUN_PATH.exists() else {}
    data["whatsapp_last_run"] = dt.isoformat()
    write_json(LAST_RUN_PATH, data)


def compute_portal_cutoff() -> datetime:
    last_run = read_last_run()
    cutoff = retention_cutoff()
    if last_run is not None:
        if last_run.tzinfo is not None:
            last_run = last_run.astimezone(IST).replace(tzinfo=None)
        return max(last_run, cutoff)
    return cutoff


def load_archive(path: Path | None = None) -> list[dict]:
    path = path or ARCHIVE_PATH
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def merge_into_archive(new_messages: list[dict], path: Path | None = None) -> list[dict]:
    existing = load_archive(path)
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []
    for m in existing + new_messages:
        msg_id = m.get("id")
        if not msg_id and "timestamp" in m:
            msg_id = hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()
        if msg_id:
            by_id[msg_id] = m
        else:
            print(f"Warning: message with empty id cannot be deduplicated: {m.get('title', '<no title>')}")
            no_id.append(m)
    merged = list(by_id.values()) + no_id
    merged.sort(key=lambda m: message_timestamp(m), reverse=True)
    return merged


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(data, temporary, indent=2, ensure_ascii=False)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_archive(messages: list[dict], path: Path | None = None) -> None:
    write_json(path or ARCHIVE_PATH, messages)


def message_timestamp(message: dict) -> str:
    return message.get("posted_at") or message["timestamp"]


def _read_retention_days() -> int:
    raw = os.getenv("ARCHIVE_RETENTION_DAYS", "35")
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("ARCHIVE_RETENTION_DAYS must be a positive integer") from exc
    if days < 1:
        raise ValueError("ARCHIVE_RETENTION_DAYS must be a positive integer")
    return days


RETENTION_DAYS = _read_retention_days()
IST = ZoneInfo("Asia/Kolkata")


def retention_cutoff(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST).replace(tzinfo=None)
    return now - timedelta(days=RETENTION_DAYS)


def is_within_retention(posted_at: str, now: datetime | None = None) -> bool:
    posted = dateparser.parse(posted_at)
    if posted.tzinfo is not None:
        posted = posted.astimezone(IST).replace(tzinfo=None)
    return posted >= retention_cutoff(now)


def delete_attachments(messages: list[dict]) -> None:
    for m in messages:
        for att in m.get("attachments", []):
            saved_as = att.get("saved_as")
            if saved_as and Path(saved_as).exists():
                Path(saved_as).unlink()


def prune_archive(messages: list[dict], now: datetime | None = None,
                  delete_files: bool = True) -> list[dict]:
    kept = []
    dropped = []
    for m in messages:
        if is_within_retention(message_timestamp(m), now):
            kept.append(m)
        else:
            dropped.append(m)
    retained_paths = {
        attachment.get("saved_as")
        for message in kept for attachment in message.get("attachments", [])
    }
    for message in dropped if delete_files else []:
        delete_attachments([{
            "attachments": [attachment for attachment in message.get("attachments", [])
                            if attachment.get("saved_as") not in retained_paths]
        }])
    return kept


def cleanup_stored_data(now: datetime | None = None) -> dict[str, list[dict]]:
    if now is None:
        now = datetime.now(IST)
    archives: dict[str, list[dict]] = {}
    old_messages: list[dict] = []
    for source, path in (("portal", ARCHIVE_PATH), ("whatsapp", WHATSAPP_ARCHIVE_PATH)):
        messages = load_archive(path)
        kept = prune_archive(messages, now, delete_files=False)
        old_messages.extend(message for message in messages if message not in kept)
        if messages != kept:
            save_archive(kept, path)
        archives[source] = kept

    retained_paths = {
        Path(attachment["saved_as"]).resolve()
        for messages in archives.values() for message in messages
        for attachment in message.get("attachments", []) if attachment.get("saved_as")
    }
    pdf_dir = (DATA_DIR / "pdfs").resolve()
    retained_hashes = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in retained_paths if path.is_relative_to(pdf_dir) and path.is_file()
    }
    cache_dir = DATA_DIR / "pdf_cache"
    retained_cache_paths = set()
    for path in cache_dir.glob("*.json"):
        if path.name.split("_", 1)[0] in retained_hashes:
            retained_cache_paths.add(path.resolve())
        else:
            path.unlink()
    for message in old_messages:
        for attachment in message.get("attachments", []):
            if attachment.get("saved_as"):
                path = Path(attachment["saved_as"]).resolve()
                if path.is_relative_to(pdf_dir) and path not in retained_paths:
                    path.unlink(missing_ok=True)

    input_path = OUTPUT_DIR / "daily_brief_input.json"
    if input_path.exists():
        data = json.loads(input_path.read_text(encoding="utf-8"))
        for source_data in data.values():
            source_data["messages"] = prune_archive(source_data.get("messages", []), now, delete_files=False)
        write_json(input_path, data)

    aware_now = now.replace(tzinfo=IST) if now.tzinfo is None else now
    oldest_timestamp = (aware_now - timedelta(days=RETENTION_DAYS)).timestamp()
    protected = {ARCHIVE_PATH.resolve(), WHATSAPP_ARCHIVE_PATH.resolve(), LAST_RUN_PATH.resolve()}
    for directory in (DATA_DIR, OUTPUT_DIR):
        for path in directory.rglob("*"):
            if (path.is_file() and not path.is_symlink()
                    and path.resolve() not in protected | retained_paths | retained_cache_paths
                    and path.stat().st_mtime < oldest_timestamp):
                path.unlink()
    return archives


if __name__ == "__main__":
    cleanup_stored_data()
    print(f"Cleaned stored data older than {RETENTION_DAYS} days.")
