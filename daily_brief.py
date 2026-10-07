"""
Collects school messages, generates a validated brief with Azure OpenAI,
and renders/sends it using the existing email scripts.

Each source is best-effort: if one fails, its `error` field is set and
`messages` is empty, so Azure can still produce a partial brief
rather than nothing.

WhatsApp always uses cutoff.month_anchor() (its Drive log already
retains its own full history, so re-scanning the current month is
enough, and it stays intentionally decoupled from the portal's cursor).
The portal uses portal_archive.py's cursor-or-month-anchor cutoff, merges
newly-fetched messages into the git-committed archive
(data/portal_messages.json), and always returns the FULL merged archive
here - not just the newest fetch - so nothing scraped in the past is
ever missed. The cursor (data/last_run.json) and archive are only
updated when the portal fetch itself succeeds. Portal attachments are
downloaded into data/pdfs/ (skipping already-downloaded files).

Note: data/ must be committed and pushed after this runs for the
archive/cursor to persist across a scheduled cloud routine's fresh
clones - use --commit-archive for that environment. Local persistent
installations do not need Git commits for archive persistence.

USAGE:
    python daily_brief.py
    python daily_brief.py --preview
    python daily_brief.py --from-input --preview
    python daily_brief.py --gather-only
"""

import argparse
import io
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import commit_archive
import generate_brief
import send_email
from cutoff import month_anchor
from fetch_whatsapp import fetch_recent_whatsapp_messages
from portal_archive import compute_portal_cutoff, merge_into_archive, prune_archive, save_archive, save_last_run
from scrape_udt import fetch_recent_messages

OUTPUT_PATH = Path(__file__).parent / "output" / "daily_brief_input.json"


class SourceResult(TypedDict):
    messages: list[Any]
    error: str | None


def gather() -> dict[str, SourceResult]:
    run_started_at = datetime.now()  # noqa: DTZ005 - naive on purpose: fetch_recent_messages compares it against naive portal timestamps; fetch_recent_whatsapp_messages attaches tz itself when needed
    result: dict[str, SourceResult] = {
        "portal": {"messages": [], "error": None},
        "whatsapp": {"messages": [], "error": None},
    }

    try:
        portal_cutoff = compute_portal_cutoff()
        new_messages = fetch_recent_messages(cutoff=portal_cutoff, download_attachments=True)
        full_archive = merge_into_archive(new_messages)
        pruned_archive = prune_archive(full_archive)
        save_archive(pruned_archive)
        save_last_run(run_started_at)
        result["portal"]["messages"] = pruned_archive
    except Exception as e:  # noqa: BLE001 - best-effort per source, see module docstring
        result["portal"]["error"] = f"{type(e).__name__}: {e}"

    try:
        result["whatsapp"]["messages"] = fetch_recent_whatsapp_messages(
            cutoff=month_anchor(months_back=3)
        )
    except Exception as e:  # noqa: BLE001 - best-effort per source, see module docstring
        result["whatsapp"]["error"] = f"{type(e).__name__}: {e}"

    return result


def run():
    data = gather()
    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Wrote {OUTPUT_PATH}")
    return data


def run_pipeline(preview: bool = False, from_input: bool = False, persist_archive: bool = False) -> bool:
    now = datetime.now(generate_brief.IST)
    data: dict[str, Any] = {}
    warnings = []
    succeeded = True
    try:
        if from_input:
            data = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        else:
            data = run()
        if persist_archive and not from_input:
            output = io.StringIO()
            with redirect_stdout(output):
                commit_archive.commit_and_push()
            print(output.getvalue(), end="")
            if "WARNING:" in output.getvalue():
                warnings.append("Archive changes could not be pushed; the next fresh-clone run may miss today's update.")
        content = generate_brief.generate(data, now=now)
    except Exception as error:
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
    content.warnings = list(dict.fromkeys(content.warnings + warnings))
    generate_brief.write_content(content)
    print(f"Wrote {generate_brief.CONTENT_PATH}")
    send_email.run(preview=preview)
    return succeeded


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the daily school brief with Azure OpenAI.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gather-only", action="store_true", help="Collect messages without calling Azure or sending email.")
    mode.add_argument("--from-input", action="store_true", help="Reuse saved input without fetching messages or committing data.")
    parser.add_argument("--preview", action="store_true", help="Write HTML without sending email.")
    parser.add_argument("--commit-archive", action="store_true", help="Commit/push data after collection for fresh-clone schedules.")
    args = parser.parse_args()
    if args.gather_only:
        run()
        return 0
    return 0 if run_pipeline(args.preview, args.from_input, args.commit_archive) else 1


if __name__ == "__main__":
    raise SystemExit(main())
