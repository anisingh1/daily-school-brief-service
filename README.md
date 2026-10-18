# Daily School Brief

Pulls together messages from the school web portal and the school WhatsApp
group directly through Evolution API, and
produces a daily brief covering homework, tomorrow's agenda, tomorrow's
dress code, and other reminders using direct Azure OpenAI calls. Claude Code
and agent skills are not required to run the pipeline.

See `docs/superpowers/specs/2026-09-05-daily-school-brief-design.md` for the
original design and `docs/superpowers/plans/2026-09-05-daily-school-brief.md`
for the historical implementation plan. Those documents describe earlier
versions; the current workflow and run instructions are below.

## Current Workflow

1. Start the REST service and call `POST /initialize` to populate local storage.
2. The in-process cron scheduler collects messages on the configured schedule.
  Each source uses its own last successful fetch cursor within the configured
  lookback window, including Evolution. Both sources are merged into local
  JSON archives.
3. Missing PDFs are downloaded. New or changed PDFs are transcribed with Azure
  using extracted text and page images. Full-page transcriptions are cached by
  file hash and extraction version, so unchanged PDFs are not uploaded again.
4. Collection and brief generation apply retention cleanup, defaulting to 35
  days. Retained message timestamps determine which messages and PDFs stay.
5. After collection and PDF preparation, the same cron job generates the brief
  only when its inputs or date context have changed. Otherwise it reuses the
  last validated brief without an Azure generation request.
  Call `GET /brief` (or `POST /brief`) to read it without any Azure calls.
  Use `POST /refresh` to run the complete cron workflow immediately.
6. Consume the saved JSON through `/brief`; email rendering, SMTP delivery,
  and email-based WhatsApp capture are no longer supported.

Storage is local files, not a database. No Claude runtime, agent framework,
OS cron job, or automatic Git commit is needed. Keep `data/` persistent and run
one service process against it.

The REST workflow combines scheduled collection and generation, with separate
saved-content reads. Running
`daily_brief.py` directly performs collection and change-aware generation using
the same shared implementations. It is an alternative to the service, not a
second process to run alongside it against the same files.

## Build And Setup

For the complete four-container setup, use
[Docker setup and native debugging](docs/DOCKER_SETUP.md). Once configured:

```bash
docker compose up -d --build --wait --wait-timeout 180
```

Docker runs Python, patched Evolution, PostgreSQL, and Redis with persistent
volumes. Native development remains supported; the instructions below describe
that alternative. Do not run native and Docker copies simultaneously on the
same ports or WhatsApp login.

Run these commands from the repository root. Python 3.11 has been tested
locally and is recommended. This is a Python application with no separate
compilation or frontend build step.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a new installation only, create your local configuration:

```bash
cp .env.example .env
```

Do not overwrite an existing `.env`. Fill in these settings:

- `UDT_BASE_URL`, `UDT_USERNAME`, `UDT_PASSWORD`: school portal access.
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
  `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_API_KEY`: your Azure deployment.
  Use the deployment name, not the model family name. It must support Chat
  Completions, strict JSON-schema structured outputs, and image input.
- Configure `EVOLUTION_API_URL`,
  `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE`, and `EVOLUTION_GROUP_JID`.
  Follow [docs/EVOLUTION_SETUP.md](docs/EVOLUTION_SETUP.md) for native installation,
  QR pairing, group discovery, and verification. Evolution uses an unofficial
  WhatsApp Web connection; review the account and privacy risks before linking.

Old SMTP, recipient, IMAP, and phone-capture settings are unused and can be
removed from an existing `.env`. `WHATSAPP_SOURCE` is unnecessary; if retained,
it must be `evolution`. Old `email` values fail explicitly rather than silently
switching sources. Previously imported messages remain until normal retention
removes them.

The schedule and retention settings are shown below. Retention must be a
positive integer. Keep `.env` private and never commit it. School messages and
PDF pages are sent to your configured Azure endpoint; review data-handling
requirements before running. Azure requests use a 120-second timeout and at
most two SDK retries.

## Scheduled Service

After setup, start the service in a terminal from the repository root:

```bash
.venv/bin/python service.py
```

The default address is `http://127.0.0.1:8000`; interactive API documentation
is at `/docs`. The service runs one process with an in-memory scheduler and
the existing JSON/PDF files, not a database. Configure it in `.env`:

```dotenv
MESSAGE_POLL_CRON="*/30 * * * *"
MESSAGE_POLL_TIMEZONE=Asia/Kolkata
ARCHIVE_RETENTION_DAYS=35
```

`ARCHIVE_RETENTION_DAYS` controls both how far back initialization, scheduled
collection, source endpoints, and the full CLI pipeline fetch UDT and WhatsApp
messages, and how long stored data is retained. For example, set it to `7` to
fetch and retain at most seven days. Restart the service after changing `.env`.

`MESSAGE_POLL_CRON` is a standard five-field cron expression (minute, hour,
day of month, month, day of week). For example, `0 18 * * *` collects at
18:00 daily. Numeric weekday `0`/`7` means Sunday, and day-of-month and
day-of-week restrictions use standard cron OR semantics. Restart the service
after changing configuration. It polls only while the process is running;
there is no OS cron installation or automatic startup configuration.

Initialize storage once after starting, or repeat safely to refresh messages
within the configured lookback window:

```bash
curl -X POST http://127.0.0.1:8000/initialize
```

This ignores both source cursors, filters exposed messages to the lookback
window before saving attachments, downloads missing
PDFs while reusing existing files in `data/pdfs/`, and creates the message JSON
archives and cached PDF transcriptions. It also collects captured WhatsApp
messages within the same lookback window. Older messages/PDFs are removed by
retention cleanup, even during initialization. It does not generate a brief.
The response reports retained message/document counts and warnings.
The portal currently exposes one activity page; unavailable historical messages
cannot be recovered by this endpoint. PDF transcription uses the configured Azure
deployment and may take time on the initial scan.

Initialization, scheduled polling, `POST /refresh`, and `POST /scrape-udt` also
fetch the portal's dedicated attendance endpoint for each month in the retention
window, including the complete current month. Records are stored in
`data/attendance.json` and included in `output/daily_brief_input.json`.
Attendance is fetched independently of message cursors so corrected statuses are
picked up on later runs. A failed attendance fetch reuses retained records and
reports a warning without blocking homework collection. Collection responses
report a separate `attendance_records` count. WhatsApp-only collection does not
fetch attendance.

At each cron tick, the service fetches new portal and WhatsApp messages using
their separate cursors, stores text/PDFs, prepares PDF caches,
applies cleanup, and generates or reuses the saved brief.
Refresh errors are visible in `GET /health`; failed generation does not replace
the last successfully saved brief.
Starting the service does not immediately fetch or initialize storage.
`GET /brief` and `POST /brief` return the previously generated brief. Freshness
is bounded by the last successful refresh. Use `POST /refresh` when you need an
immediate refresh between scheduled polls. A best-effort source fetch failure
uses retained messages with a warning rather than claiming fresh data.

Collect just one source on demand:

```bash
curl -X POST http://127.0.0.1:8000/scrape-udt
curl -X POST http://127.0.0.1:8000/fetch-whatsapp
```

Portal and Evolution fetch messages strictly newer than that source's last
successful collection start time, bounded by the configured lookback window. Without a
saved cursor, it uses that window directly. Messages and attachments are saved locally and PDF caches are prepared;
neither endpoint generates the final brief. New/changed PDFs may still require
Azure transcription. The response includes retained
message counts and PDF document counts. The other source is not fetched.
Cursors advance only after successful fetching and archive persistence, so failed
fetches or archive writes can be retried. `POST /initialize` bypasses both cursors
to backfill storage. All collection routes share the scheduler's storage lock.

Run the exact cron workflow manually, including collection and generation:

```bash
curl -X POST http://127.0.0.1:8000/refresh
```

This synchronous endpoint fetches both sources, stores messages/PDFs, prepares
caches, and generates or reuses `output/daily_brief_content.json`. It returns
collection counts, warnings, `brief_generated`, `brief_reused`, and
`duration_seconds`. A cache hit returns `brief_generated: false` and
`brief_reused: true`; read the unchanged content through `/brief`.
Without initialized storage, collection uses the configured
lookback window. Failures return HTTP 502; overlapping storage operations return
HTTP 409. This endpoint can take as long as the scheduled workflow.

Warnings are exposed only in `/initialize` and `/refresh` responses, not in
`/brief` or `/health`. Refresh warnings include both collection
and brief-generation warnings. Scheduled runs log each warning at WARNING level;
saved content retains warnings for internal diagnostics.

Read the saved brief without collection, cleanup, or LLM calls:

```bash
curl http://127.0.0.1:8000/brief
# Existing clients can still use:
curl -X POST http://127.0.0.1:8000/brief
```

Both methods return the validated brief JSON without internal warnings. Missing content returns
HTTP 409 with a prompt to run `POST /refresh`; invalid content returns HTTP 502.
The storage lock covers the complete refresh workflow, preventing overlapping
collection or generation. Brief reads do not acquire this lock and can return
the previous saved result during refresh. Brief content and PDF caches are
written atomically so readers do not see partially written JSON.

Calendar planners are read visually: red-background date cells are transcribed
as explicitly dated school holidays, even without a printed holiday label.
An explicit color legend takes precedence; unclear dates or colors produce
warnings. Confirmed holidays suppress that day's school dress/activity packing
instructions unless an explicit attendance instruction overrides the closure.
Saturdays and Sundays are always regular school holidays and are omitted from
the daily and calendar `holidays` lists, even when explicitly marked in a notice.
The generation prompt also omits reminders announcing weekend holidays, while
preserving genuine weekend assignments, deadlines, and events.
PDF cache version `v3` refreshes older extractions to preserve these visual cues.

For a date-selecting frontend, use the response's `calendar` array. It contains
one ISO-dated entry for every day of the current month in IST, in date order,
including days with no known content. Each entry has `date`, `classwork`,
`homework`, `homework_pages`, `agenda`, `reminders`, `holidays`, and `attendance`.
Attendance is `"present"`, `"absent"`, `"leave"`, or `null` when no record exists.
It comes directly from portal records, never from model inference or whether a
day has homework. Explicitly recorded future leave is preserved. Older saved
calendar entries without attendance default to `null`.
Agenda, reminders, and holidays include known planner/notice facts throughout
the month, not just tomorrow or the next 14 days. Reminders are attached to
the date an action is required. Notice overrides are resolved per affected date.
Homework, classwork, and homework PDF links are tied to their actual school
dates; Python always clears these fields on future dates. Unknown content stays
empty rather than being predicted or copied from the daily summary.

Example calendar entry (the actual array contains every date in the month):

```json
{
  "calendar": [
    {
      "date": "2026-10-25",
      "classwork": [],
      "homework": [],
      "homework_pages": [],
      "agenda": ["Half-day dismissal at 12:00"],
      "reminders": ["Route 4 pickup changes to 07:30"],
      "holidays": [],
      "attendance": null
    }
  ]
}
```

The top-level fields retain their daily-summary behavior, including tomorrow's
agenda and the 14-day reminder window (tomorrow through today plus 14 days,
inclusive); the calendar is the monthly frontend
view. It is saved with the brief JSON.
Older saved briefs without `calendar` remain readable. The frontend should cache
older months itself: this response covers the current month using retained
sources only. PDF links are subject to server retention, so retaining a link
alone does not preserve the PDF file after cleanup.

Newer explicit school notices from the portal or WhatsApp override older planner
entries for the specified dates, class, and bus route. Closure and reopening
announcements update the holiday list. Half-day dismissal and bus pickup/drop-off
changes appear in tomorrow's agenda or the 14-day reminder window;
they are not classified as full-day holidays. Relative dates in notices are
resolved using the notice timestamp. Unresolved conflicts produce warnings.

Reminders explicitly cover items to send to school, fee payments, and consent
or permission requests, preserving supplied quantities, amounts/currency,
submission methods, recipients, required documents, and deadline dates/times.
Actions due within the 14-day window are included even when their event is later;
action deadlines and event dates remain distinct. Recent applicable requests
without deadlines are marked "Deadline not specified", without inventing a date
or assuming a payment or consent is outstanding. Explicitly completed, cancelled,
superseded, and expired actions are omitted. Dated calendar reminders use the
action deadline; undated requests are not assigned an invented calendar date.

The response includes a separate `holidays` list, also saved in the brief JSON
and contains confirmed full-day closures from today
onward, including known planner dates beyond the reminder window. Dates are
validated, deduplicated, and sorted; past or cancelled holidays are excluded.
The list is rebuilt from retained sources on each generation, not maintained in
a separate calendar file. Older saved briefs without this field remain readable.

```json
{
  "holidays": [
    {"date": "2026-10-09", "reason": "School closed due to rain"}
  ]
}
```

PDF extraction also classifies each document as `school_notice` (assembly
schedules, monthly activity planners, weekly updates and event notices) or
`notebook_page` (notebook, workbook or worksheet exercises). Classification is
cached during collection; existing unclassified caches are rebuilt as needed.
The brief's `homework_pages` contains all `notebook_page` PDFs sent today in IST,
including classwork and homework, plus older notebook PDFs relevant to its selected
homework. Today's links are included even if the model omits them. Each PDF appears
once with its exact `name` and `url`; school notices and unrelated older notebook
pages are not included. The field name is retained for API compatibility.

```json
{
  "homework_pages": [
    {"name": "Hindi worksheet.pdf", "url": "/pdfs/Hindi%20worksheet.pdf"}
  ]
}
```

Links always use downloaded PDFs via `GET /pdfs/{filename}` on this service.
PDF downloads require no authentication; anyone who can reach the service and
knows the URL can access them. Other API routes retain their authentication. Relative links are
resolved against your service URL. Set `SERVICE_PUBLIC_URL` to your trusted,
reachable HTTP(S) service URL to produce absolute links for API consumers.
Files must remain under `data/pdfs`; links stop working after retention cleanup.

`GET /health` reports the schedule, next poll, retention, initialization state,
and the last scheduled or manual refresh result, including generation status.
Its `busy` flag reports the storage lock, including active Azure generation.
A conflicting storage operation returns HTTP 409; a cron poll skips while the
lock is held. Saved brief reads remain available. Use one
service process/worker and do not run the standalone collection/cleanup commands
against the same files concurrently. Shutdown waits for active scheduled work.
For always-on operation, keep the service running under your process supervisor.

Check the running service:

```bash
curl --fail http://127.0.0.1:8000/health
```

Open `http://127.0.0.1:8000/docs` to try the endpoints interactively. Stop the
service with Ctrl-C. Restart it after editing `.env`. The default localhost
binding needs no host, port, or token settings. Optional environment overrides
remain supported in code for remote access: a non-local binding requires a
bearer token, and remote exposure should use HTTPS through a reverse proxy.
The service does not commit or push archives automatically.

### Persistent brief reuse

`output/daily_brief_content.cache.json` records the input fingerprint and a
checksum of the validated saved brief. Reuse survives process restarts and does
not depend on in-memory state. The shared CLI/service generator checks:

- Retained messages, source errors, attachment metadata, and attendance records.
- PDF bytes and validated cached transcriptions, including extraction version.
- IST date context: midnight and the 08:30 dress-code cutoff invalidate reuse.
- Generation implementation, prompt/schema, Azure endpoint/deployment/API version,
  and `SERVICE_PUBLIC_URL`. API keys are not stored in cache metadata.

Collection cursors, poll timestamps, and file modification times are not inputs.
An unchanged refresh still polls for new messages and attendance, but skips
Azure generation; existing PDF transcriptions also avoid extraction requests.
Missing/corrupt content or metadata is regenerated, never returned as a cache
hit. Incomplete PDF preparation prevents brief reuse so later refreshes can retry.
Failed generation leaves the last successful brief and its metadata untouched.
The first run after upgrading has no trusted fingerprint and generates once.
Delete only the cache metadata to force generation on the next refresh.

### Automatic startup and restart on macOS

The application does not install an OS service. For unattended operation, use
two per-user `launchd` LaunchAgents: one for the Python service and one for the
native Evolution process. This is a recommended approach, not installed by
this repository:

- Put job property lists in `~/Library/LaunchAgents`, with unique labels,
  `RunAtLoad`, `KeepAlive`, and restart throttling such as `ThrottleInterval=30`.
- Use absolute executable paths: the project's `.venv/bin/python` with
  `service.py`, and the installed Node executable with Evolution's `dist/main`.
  Set each `WorkingDirectory` to its repository, because both use local config.
  Do not rely on interactive shell activation, npm shims, or shell profiles.
- Explicitly set `SERVICE_RELOAD=false` and use a single Python process/worker.
  Configure private stdout/stderr log paths and log rotation.
- Keep the existing shared PostgreSQL and Redis available under their own
  supervision. launchd does not guarantee dependency startup ordering; Evolution
  must retry/restart if those dependencies are not yet ready.
- Stop existing debugger/terminal processes before loading the jobs to avoid
  duplicate schedulers or occupied ports. Verify `/health`, Evolution readiness,
  and `evolution_admin.py status`; then test a controlled process restart.

LaunchAgents start at user login, not before login, and do not keep a sleeping
Mac collecting messages. A continuously awake host is needed for reliable
scheduled polling. No startup jobs or running processes were changed as part
of this explanation.

## Components

- **`scrape_udt.py`** — logs into the UDT eSchool parent portal, fetches
  the activity/messages page, parses each message (title, author, date,
  body, PDF attachments), and filters to a rolling lookback window (default
  36 hours) when run standalone. Downloads PDF attachments into
  `data/pdfs/`, skipping any file that's already been saved there (by
  message ID + name), so the same document isn't re-downloaded every
  run. Can be run directly (`python scrape_udt.py`), or imported
  (`fetch_recent_messages()`) for use by other scripts.
- **`fetch_whatsapp.py`** — Evolution-only entrypoint; delegates text, caption,
  and PDF ingestion to `fetch_evolution.py`. Uses a rolling lookback standalone,
  or the saved cursor in the service bounded by `ARCHIVE_RETENTION_DAYS`.
  See [docs/EVOLUTION_SETUP.md](docs/EVOLUTION_SETUP.md) for setup.
- **`portal_archive.py`** - persists portal and WhatsApp message archives
  as local JSON, plus each source's last successful fetch cursor. New
  messages are fetched since their cursor, bounded by the retention window.
  WhatsApp messages are deduplicated by their captured message content.
  Both archives, PDFs, extracted PDF text, and saved outputs are cleaned up
  locally without a database.
- **`daily_brief.py`** — orchestrator that calls both fetchers (portal
  via their independent `portal_archive.py` cursors),
  treating each as best-effort (one source failing doesn't block the
  other), downloads portal attachments, and writes a combined JSON
  envelope to `output/daily_brief_input.json`. It then calls Azure generation,
  writes validated content or reuses the unchanged saved brief.
  The `gather()` and `run()` Python functions still support collection alone.
- **`service.py`** - long-running cron collection and brief generation, plus REST
  endpoints for initialization, independent source collection, manual refresh,
  saved brief reads, and service health. Scheduling and endpoints reuse the same
  collection and generation implementations as the CLI.
- **`generate_brief.py`** - reads stored messages and PDFs. New or changed
  PDFs are transcribed by Azure using extracted text and rendered page images,
  preserving dates, scans, and student-to-assignment table rows. Validated
  page text is cached by SHA-256 and extraction version under `data/pdf_cache/`.
  Unchanged PDFs reuse that text without another PDF/image request. A separate
  daily Azure call applies the school rules and validates the structured brief
  using Pydantic. Dates are computed in IST by Python.
- `.claude/skills/daily-school-brief/SKILL.md` is only a compatibility launcher
  for older agent-based triggers. It does not interpret messages or generate
  content. New schedules should invoke `python daily_brief.py` directly.

## Standalone Commands

These are alternatives to the service workflow. Stop the service before
running collection, generation, or cleanup commands against the same storage.
Standalone fetchers use a built-in 36-hour lookback; the service and full
pipeline use their explicit cursor and `ARCHIVE_RETENTION_DAYS` cutoffs instead.

- Portal scraper only (saves messages + downloads PDFs):
  ```
  .venv/bin/python scrape_udt.py
  ```
- WhatsApp fetcher only (prints recent messages):
  ```
  .venv/bin/python fetch_whatsapp.py
  ```
- Full pipeline (collects messages, generates or reuses the saved JSON brief):
  ```
  .venv/bin/python daily_brief.py
  ```
- Generate or reuse a brief from the latest stored archives/PDFs without fetching:
  ```
  .venv/bin/python daily_brief.py --from-input
  ```
  Despite the legacy flag name, messages come from the retained archives, not
  the message snapshot in `output/daily_brief_input.json`. The saved input is
  consulted only for source-error metadata.
- Collection only (no Azure call):
  ```
  .venv/bin/python daily_brief.py --gather-only
  ```
- Generate content only from the latest stored archives:
  ```
  .venv/bin/python generate_brief.py
  ```
- The obsolete `--commit-archive` option and Git-publishing helper have been
  removed. Store `data/` on persistent local storage or a mounted volume; school
  data is ignored by Git. Historical design documents describe the retired
  fresh-clone workflow, not the current runtime.

- Cleanup only, without downloading or calling Azure:
  ```
  .venv/bin/python portal_archive.py
  ```
  Cleanup also runs before and after normal collection and before `--from-input`.
  Generation reads the cleaned, retained archives.

If collection raises an unrecoverable error or Azure generation fails, the pipeline
preserves the previous saved brief and exits nonzero. Source
failures and unreadable PDFs appear as warnings; another source can still be
used. Invalid JSON, refusals, and truncated completions are never saved as a
normal brief. Empty sources skip Azure entirely. Oversized requests
fail explicitly rather than silently discarding messages or pages.

## Verify

Run the automated tests without fetching live messages or calling Azure:

```bash
.venv/bin/python -m pytest -q
```

For just the REST service and scheduler tests:

```bash
.venv/bin/python -m pytest tests/test_service.py -q
```

## Output

- `output/messages_<timestamp>.json` — all parsed portal messages within
  the lookback window, with title, author/date, body text, and attachment
  info (written by `scrape_udt.py`'s standalone `run()`). Not committed
  (gitignored, under `output/`).
- `output/daily_brief_input.json` — combined `{"portal": {...}, "whatsapp":
  {...}}` envelope written by `daily_brief.py`, each side having
  `messages` and `error` fields. A collection snapshot and source-error metadata,
  not the authoritative message store for generation. Not committed.
- `output/daily_brief_content.json` — Azure's validated structured categorization
  (`date`, `warnings`, `classwork`, `homework`, `homework_pages`, `agenda`,
  `dress_code`, `reminders`, `holidays`, `calendar`), served by `/brief`. Not committed.
- `output/daily_brief_content.cache.json` - persistent generation fingerprint
  and saved-content checksum; no messages or credentials are stored here.
- Aviraj-specific notices and assignments appear in `reminders`, not in a
  separate highlight section. Legacy saved highlights are moved into reminders
  when read, without duplicating identical reminder text.
- `data/portal_messages.json` - the rolling 35-day portal message archive,
  deduplicated by message id. Stored locally, ignored by Git.
- `data/whatsapp_messages.json` - the rolling 35-day captured WhatsApp message
  archive, including text and saved PDF references. Stored locally, ignored by Git.
- `data/pdf_cache/` - validated full-page PDF transcriptions, keyed by content
  hash and extraction version. Corrupt entries are rebuilt on the next use.
- `data/last_run.json` — independent successful collection timestamps under
  `last_run` (portal) and `whatsapp_last_run`. Stored locally, ignored by Git.
- `data/pdfs/` — downloaded PDF (or other) attachments, named using the
  message ID and their display name in the portal (prefixed `whatsapp_`
  for ones from the WhatsApp document capture instead). Stored locally and
  ignored by Git; a file already present here is not re-downloaded.

## Notes

- All portal messages currently load on a single page load (no pagination
  or infinite-scroll fetching) — if the school portal changes this in the
  future, the script will silently only see what's in that first page
  load and may need extending.
- Portal login is a plain form POST to `/Logins/index` with `username`/
  `password` fields — if the school changes their login page (adds a
  CSRF token, captcha, etc.) this script will need updating to match.
- PDF attachments are fetched by following each attachment's viewer page
  and extracting the real file URL from an embedded `file_path` JS
  variable, then downloading that URL directly (skipped if already
  downloaded).
- The scheduler polls a self-hosted Evolution
  API v2 instance. It imports only the configured group's text, captions, and
  PDF documents; images, audio, video, and other document binaries are not imported.
  Normal collection only queries messages newer than the last successful
  WhatsApp run, bounded by retention. Stable IDs prevent duplicates. A disconnected instance or malformed
  response fails that collection explicitly and leaves the prior archive/cursor
  intact. Unavailable PDFs retain their message and attachment metadata with a
  visible download warning and are not retried by normal polls. Use explicit
  initialization to recheck history and retry unavailable PDFs. Late-synced
  messages dated before the cursor also require initialization. Invalid PDF content
  and local storage failures still fail collection. No public webhook endpoint
  is required.
- Retention defaults to 35 days and is controlled by `ARCHIVE_RETENTION_DAYS`.
  Portal and WhatsApp messages older than that window are removed from their
  JSON archives and saved input. Age uses the source message timestamp in IST,
  not the download date; messages exactly at the cutoff are retained. PDFs belonging only
  to expired messages are deleted, together with their extraction caches.
  A PDF shared with a retained message is kept. Unreferenced files under
  `data/` and old files under `output/` are removed after the retention window by modification
  time. Archives and the operational fetch cursor are retained as metadata.
  External portal/Evolution history is not deleted. Stored data remains available
  when a source fetch fails, with an explicit warning in the brief.
  Deleting a file doesn't reclaim git history size on its own though —
  the blob still exists in past commits until a history rewrite
  (`git gc`, BFG, `git filter-repo`), so repo size still grows slowly
  over time even with pruning in place, just far more slowly than
  without it.
