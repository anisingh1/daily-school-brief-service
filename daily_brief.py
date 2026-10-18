"""
Collects school messages, generates a validated brief with Azure OpenAI,
and renders/sends it using the existing email scripts.

Each source is best-effort: on failure, its `error` field is set and
retained local messages are still used. Both sources are stored as JSON
archives under data/ and attachments are downloaded into data/pdfs/.
Each source uses its last successful fetch cursor, falling back to the
retention window. Cleanup runs before and after collection, even
when a fetch fails, and before reusing saved input. The brief uses the
merged retained messages, not just the latest fetch.

Storage is local and ignored by Git. Keep data/ on persistent storage.

USAGE:
    python daily_brief.py
    python daily_brief.py --preview
    python daily_brief.py --from-input --preview
    python daily_brief.py --gather-only
"""

import argparse
import json
from calendar import monthrange
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import generate_brief
import portal_archive
import send_email
from fetch_whatsapp import fetch_recent_whatsapp_messages
from portal_archive import (
    compute_portal_cutoff,
    merge_into_archive,
    prune_archive,
    save_archive,
    save_last_run,
)
from scrape_udt import fetch_attendance, fetch_recent_messages

OUTPUT_PATH = Path(__file__).parent / "output" / "daily_brief_input.json"


class SourceResult(TypedDict):
    messages: list[Any]
    error: str | None
    records: NotRequired[list[dict]]


def gather(initialize: bool = False, source: Literal["portal", "whatsapp"] | None = None) -> dict[str, SourceResult]:
    run_started_at = datetime.now(portal_archive.IST).replace(tzinfo=None)
    stored = portal_archive.cleanup_stored_data(run_started_at)
    result: dict[str, SourceResult] = {
        "portal": {"messages": stored["portal"], "error": None},
        "whatsapp": {"messages": stored["whatsapp"], "error": None},
        "attendance": {"messages": [], "records": [], "error": None},
    }
    attendance_start = min(portal_archive.retention_cutoff(run_started_at), run_started_at.replace(day=1))
    attendance_end = run_started_at.replace(day=monthrange(run_started_at.year, run_started_at.month)[1])
    attendance_path = portal_archive.DATA_DIR / "attendance.json"
    try:
        if attendance_path.exists():
            result["attendance"]["records"] = [
                record for record in json.loads(attendance_path.read_text(encoding="utf-8"))
                if attendance_start.date().isoformat() <= record["date"] <= attendance_end.date().isoformat()
            ]
        if source in (None, "portal"):
            records = fetch_attendance(attendance_start, attendance_end)
            portal_archive.write_json(attendance_path, records)
            result["attendance"]["records"] = records
    except Exception as error:
        result["attendance"]["error"] = f"{type(error).__name__}: {error}"

    try:
        if source in (None, "portal"):
            portal_cutoff = portal_archive.retention_cutoff(run_started_at) if initialize else compute_portal_cutoff()
            if source == "portal" and not initialize:
                last_portal_run = portal_archive.read_last_run()
                if last_portal_run is not None and last_portal_run.tzinfo is not None:
                    last_portal_run = last_portal_run.astimezone(portal_archive.IST).replace(tzinfo=None)
                if last_portal_run == portal_cutoff:
                    portal_cutoff += timedelta(microseconds=1)
            new_messages = fetch_recent_messages(cutoff=portal_cutoff, download_attachments=True)
            full_archive = merge_into_archive(new_messages)
            pruned_archive = prune_archive(full_archive)
            save_archive(pruned_archive)
            save_last_run(run_started_at)
            result["portal"]["messages"] = pruned_archive
    except Exception as e:  # noqa: BLE001 - best-effort per source, see module docstring
        result["portal"]["error"] = f"{type(e).__name__}: {e}"

    try:
        if source in (None, "whatsapp"):
            last_run = None if initialize else portal_archive.read_whatsapp_last_run()
            whatsapp_cutoff = portal_archive.retention_cutoff(run_started_at).replace(tzinfo=portal_archive.IST)
            if last_run is not None:
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=portal_archive.IST)
                whatsapp_cutoff = max(whatsapp_cutoff, last_run + timedelta(microseconds=1))
            new_whatsapp = fetch_recent_whatsapp_messages(cutoff=whatsapp_cutoff)
            whatsapp_archive = portal_archive.merge_into_archive(new_whatsapp, portal_archive.WHATSAPP_ARCHIVE_PATH)
            whatsapp_archive = portal_archive.prune_archive(whatsapp_archive, run_started_at)
            portal_archive.save_archive(whatsapp_archive, portal_archive.WHATSAPP_ARCHIVE_PATH)
            portal_archive.save_whatsapp_last_run(run_started_at)
            result["whatsapp"]["messages"] = whatsapp_archive
    except Exception as e:  # noqa: BLE001 - best-effort per source, see module docstring
        result["whatsapp"]["error"] = f"{type(e).__name__}: {e}"

    portal_archive.cleanup_stored_data(run_started_at)
    return result


def run(initialize: bool = False, source: Literal["portal", "whatsapp"] | None = None):
    if source:
        data = gather(initialize=initialize, source=source)
    else:
        data = gather(initialize=True) if initialize else gather()
    if source and OUTPUT_PATH.exists():
        saved_input = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        other_source = "whatsapp" if source == "portal" else "portal"
        data[other_source]["error"] = saved_input.get(other_source, {}).get("error")
        if source == "whatsapp":
            data["attendance"]["error"] = saved_input.get("attendance", {}).get("error")
    portal_archive.write_json(OUTPUT_PATH, data)
    print(f"Wrote {OUTPUT_PATH}")
    return data


def run_pipeline(preview: bool = False, from_input: bool = False) -> bool:
    now = datetime.now(generate_brief.IST)
    data: dict[str, Any] = {}
    warnings: list[str] = []
    succeeded = True
    try:
        if from_input:
            data = generate_brief.load_stored_input(now)
        else:
            data = run()
        generate_brief.run(data=data, now=now)
    except Exception as error:  # noqa: BLE001 - pipeline failures must produce a warning-only brief
        succeeded = False
        status_code = getattr(error, "status_code", None)
        reason = type(error).__name__
        if status_code is not None:
            reason += f", HTTP {status_code}"
        warnings.extend(
            f"{source_name}: {source['error']}"
            for source_name, source in data.items() if source.get("error")
        )
        warnings.append(f"Brief generation failed ({reason}). Check source availability and Azure configuration.")
        content = generate_brief.empty_brief(generate_brief.date_context(now)["brief_date"], [])
        content.calendar = generate_brief.normalize_calendar(
            content, now.date(), {}, data.get("attendance", {}).get("records", []),
        )
        content.warnings = list(dict.fromkeys(content.warnings + warnings))
        generate_brief.write_content(content)
        print(f"Wrote {generate_brief.CONTENT_PATH}")
    send_email.run(preview=preview)
    return succeeded


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily school brief with Azure OpenAI.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gather-only", action="store_true", help="Collect messages without calling Azure or sending email.")
    mode.add_argument("--from-input", action="store_true", help="Reuse saved input without fetching messages.")
    parser.add_argument("--preview", action="store_true", help="Write HTML without sending email.")
    args = parser.parse_args()
    if args.gather_only:
        run()
        return 0
    return 0 if run_pipeline(args.preview, args.from_input) else 1


if __name__ == "__main__":
    raise SystemExit(main())
