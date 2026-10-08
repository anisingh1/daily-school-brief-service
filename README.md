# Daily School Brief

Pulls together messages from the school web portal and (via phone-forwarded
messages captured as email) the school WhatsApp group, and
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
  Portal collection uses the last successful fetch cursor; WhatsApp collection
  uses the retention window. Both sources are merged into local JSON archives.
3. Missing PDFs are downloaded. New or changed PDFs are transcribed with Azure
  using extracted text and page images. Full-page transcriptions are cached by
  file hash and extraction version, so unchanged PDFs are not uploaded again.
4. Collection and brief generation apply retention cleanup, defaulting to 35
  days. Retained message timestamps determine which messages and PDFs stay.
5. Call `POST /brief` to generate today's brief from stored messages and cached
  PDF text. Azure applies the school rules; Python validates the JSON output
  and saves it locally. This endpoint does not collect messages or send email.
6. Optionally render a preview or send the saved brief with `send_email.py`.

Storage is local files, not a database. No Claude runtime, agent framework,
OS cron job, or automatic Git commit is needed. Keep `data/` persistent and run
one service process against it.

## Build And Setup

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
- `SMTP_USERNAME`, `SMTP_APP_PASSWORD`: Gmail credentials, also used for
  WhatsApp capture over IMAP. Use a Gmail App Password with 2-Step Verification
  enabled, not the account password.
- `EMAIL_TO`: recipients for optional email delivery, comma-separated.
- `WHATSAPP_EMAIL_TO`: capture mailbox address. Complete the phone automation
  setup in [docs/PHONE_SETUP.md](docs/PHONE_SETUP.md) to enable this source.

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

`MESSAGE_POLL_CRON` is a standard five-field cron expression (minute, hour,
day of month, month, day of week). For example, `0 18 * * *` collects at
18:00 daily. Numeric weekday `0`/`7` means Sunday, and day-of-month and
day-of-week restrictions use standard cron OR semantics. Restart the service
after changing configuration. It polls only while the process is running;
there is no OS cron installation or automatic startup configuration.

Initialize storage once after starting, or repeat safely to refresh all
portal messages that the school's activity page currently exposes:

```bash
curl -X POST http://127.0.0.1:8000/initialize
```

This ignores the portal cursor, reads every exposed message, downloads missing
PDFs while reusing existing files in `data/pdfs/`, and creates the message JSON
archives and cached PDF transcriptions. It also collects captured WhatsApp
messages within the retention window. Older messages/PDFs are removed by
retention cleanup, even during initialization. It does not generate a brief
or send email. The response reports retained message/document counts and warnings.
The portal currently exposes one activity page; unavailable historical messages
cannot be recovered by this endpoint. PDF transcription uses the configured Azure
deployment and may take time on the initial scan.

At each cron tick, the service fetches new portal messages using the cursor,
refreshes retained WhatsApp messages, stores text/PDFs, prepares PDF caches,
and applies cleanup. It does not automatically generate or email a brief.
Collection errors are visible in `GET /health` and retained data stays usable.
Starting the service does not immediately fetch or initialize storage.

Generate and return a brief from stored data on demand:

```bash
curl -X POST http://127.0.0.1:8000/brief
```

This applies cleanup, reads the retained archives and cached documents, calls
Azure for the daily brief, returns the validated eight-field JSON, and writes
`output/daily_brief_content.json`. It never fetches new portal/WhatsApp messages
or sends email. Missing PDF caches are prepared as needed. Initialization is
required if no archive exists. Failures return HTTP 502 rather than stale content.

`GET /health` reports the schedule, next poll, retention, initialization state,
and last scheduled collection result. A conflicting operation returns HTTP 409;
a cron poll skips if initialization or generation is still running. Use one
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

To preview or email the saved brief after `POST /brief`:

```bash
.venv/bin/python send_email.py --preview
# Optional delivery using SMTP:
.venv/bin/python send_email.py
```

The preview is written to `output/daily_brief_preview.html`. These commands
reuse saved content without another Azure generation request. Wait for any
active service operation to finish before accessing the same output files.

## Components

- **`scrape_udt.py`** — logs into the UDT eSchool parent portal, fetches
  the activity/messages page, parses each message (title, author, date,
  body, PDF attachments), and filters to a rolling lookback window (default
  36 hours) when run standalone. Downloads PDF attachments into
  `data/pdfs/`, skipping any file that's already been saved there (by
  message ID + name), so the same document isn't re-downloaded every
  run. Can be run directly (`python scrape_udt.py`), or imported
  (`fetch_recent_messages()`) for use by other scripts.
- **`fetch_whatsapp.py`** — reads WhatsApp group messages and documents
  forwarded via phone automation as email (see `docs/PHONE_SETUP.md`),
  via IMAP using the same Gmail App Password as `send_email.py` (no
  Google Cloud project or service account), filtered to the same
  rolling lookback window (standalone) or the configured retention cutoff
  (35 days by default). PDF attachments are saved to `data/pdfs/`
  (prefixed `whatsapp_`); other attachment types are ignored.
- **`cutoff.py`** - legacy month-anchor helper; the daily pipeline now uses
  the configured retention window instead.
- **`portal_archive.py`** - persists portal and WhatsApp message archives
  as local JSON, plus the portal's last successful fetch cursor. New portal
  messages are fetched since the cursor, or within retention on first use.
  WhatsApp messages are deduplicated by their captured message content.
  Both archives, PDFs, extracted PDF text, and saved outputs are cleaned up
  locally without a database.
- **`daily_brief.py`** — orchestrator that calls both fetchers (portal
  via `portal_archive.py`'s cursor, WhatsApp via the retention cutoff),
  treating each as best-effort (one source failing doesn't block the
  other), downloads portal attachments, and writes a combined JSON
  envelope to `output/daily_brief_input.json`. It then calls Azure generation,
  writes validated content, and renders/sends the email (or writes a preview).
  The `gather()` and `run()` Python functions still support collection alone.
- **`service.py`** - long-running cron collection plus REST endpoints for
  storage initialization, stored-data brief generation, and service health.
- **`generate_brief.py`** - reads stored messages and PDFs. New or changed
  PDFs are transcribed by Azure using extracted text and rendered page images,
  preserving dates, scans, and student-to-assignment table rows. Validated
  page text is cached by SHA-256 and extraction version under `data/pdf_cache/`.
  Unchanged PDFs reuse that text without another PDF/image request. A separate
  daily Azure call applies the school rules and validates the eight-field brief
  using Pydantic. Dates are computed in IST by Python.
- **`render_email.py`** — pure function, `render_brief_html(data)`,
  turning Azure's validated categorization (`output/daily_brief_content.json`)
  into a polished, sectioned HTML email body. Deterministic and tested,
  so visual quality is consistent every day regardless of that day's
  content.
- **`send_email.py`** — reads `output/daily_brief_content.json`, renders
  it via `render_email.py`, and sends it as an HTML email via Gmail SMTP.
  `python send_email.py --preview` skips SMTP and writes
  `output/daily_brief_preview.html` instead, for local dry runs.
- **`commit_archive.py`** — stages, commits (only if there's something
  staged), and pushes `data/`, so the archive/cursor persist across
  future runs. A rejected push is reported as a printed warning, not
  raised - see its docstring.
- `.claude/skills/daily-school-brief/SKILL.md` is only a compatibility launcher
  for older agent-based triggers. It does not interpret messages or generate
  content. New schedules should invoke `python daily_brief.py` directly.

## Standalone Commands

These are alternatives to the service workflow. Stop the service before
running collection, generation, or cleanup commands against the same storage.
Standalone fetchers use a built-in 36-hour lookback; the service and full
pipeline use their explicit cursor and retention cutoffs instead.

- Portal scraper only (saves messages + downloads PDFs):
  ```
  .venv/bin/python scrape_udt.py
  ```
- WhatsApp fetcher only (prints recent messages):
  ```
  .venv/bin/python fetch_whatsapp.py
  ```
- Full pipeline (collects messages, generates the brief with Azure, and sends
  the HTML email):
  ```
  .venv/bin/python daily_brief.py
  ```
- Full pipeline without sending email:
  ```
  .venv/bin/python daily_brief.py --preview
  ```
- Regenerate from saved messages/PDFs without fetching or sending email:
  ```
  .venv/bin/python daily_brief.py --from-input --preview
  ```
- Collection only (no Azure call or email):
  ```
  .venv/bin/python daily_brief.py --gather-only
  ```
- Generate content only from existing input:
  ```
  .venv/bin/python generate_brief.py
  ```
- For a scheduled environment that clones the repo fresh each run:
  ```
  .venv/bin/python daily_brief.py --commit-archive
  ```
  This explicitly commits/pushes `data/` after generation, including any PDF
  caches created during generation, even if generation fails. A local persistent
  installation retains its archive without this flag. Configure your scheduler
  to use the project's Python environment and working directory; no Claude
  scheduler is needed. Existing schedules are not changed automatically.

- Cleanup only, without downloading, calling Azure, or sending email:
  ```
  .venv/bin/python portal_archive.py
  ```
  Cleanup also runs before and after normal collection and before `--from-input`.
  Stored input is filtered to the retention window before reuse.

If collection, archive commit, or Azure generation fails, the pipeline replaces
old content with a warning-only brief and still attempts email/preview delivery.
It exits nonzero after delivery so the scheduler can detect the failure. Source
failures and unreadable PDFs appear as warnings; another source can still be
used. Invalid JSON, refusals, and truncated completions are never sent as a
normal brief. Empty sources skip the Azure call entirely. Oversized requests
produce a failure warning rather than silently discarding messages or pages.

The sender saves `output/daily_brief_preview.html` before attempting delivery.
If an SMTP connection fails before login, check SMTP access on your network,
VPN, or firewall. After connectivity is restored, run `python send_email.py`
to send the existing brief without another Azure call. Authentication and
send-time errors are reported separately; delivery is not retried automatically
because a disconnect after submission could otherwise produce duplicate emails.

## Verify

Run the automated tests without fetching live messages, calling Azure, or
sending email:

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
  `messages` and `error` fields. Not committed.
- `output/daily_brief_content.json` — Azure's validated structured categorization
  (`date`, `warnings`, `aviraj_highlight`, `classwork`, `homework`, `agenda`,
  `dress_code`, `reminders`), consumed by `send_email.py` to render and
  send the HTML email. Not committed.
- `output/daily_brief_preview.html` - rendered HTML from a `--preview` run.
- `data/portal_messages.json` - the rolling 35-day portal message archive,
  deduplicated by message id. Committed to this repo.
- `data/whatsapp_messages.json` - the rolling 35-day captured WhatsApp message
  archive, including text and saved PDF references. Committed with `data/`.
- `data/pdf_cache/` - validated full-page PDF transcriptions, keyed by content
  hash and extraction version. Corrupt entries are rebuilt on the next use.
- `data/last_run.json` — the portal cursor (`{"last_run": "<isoformat>"}`).
  Committed to this repo.
- `data/pdfs/` — downloaded PDF (or other) attachments, named using the
  message ID and their display name in the portal (prefixed `whatsapp_`
  for ones from the WhatsApp document capture instead). Committed to
  this repo; a file already present here is not re-downloaded.

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
- WhatsApp text messages are captured entirely through Android's normal
  notification system (via phone automation), not through any unofficial
  WhatsApp client library — see the design spec for why. WhatsApp
  document attachments are captured by watching WhatsApp's own
  auto-save folder for new files - this folder isn't scoped to a
  single chat, so a document from *any* WhatsApp chat on the phone gets
  emailed and considered (non-PDF attachments are filtered out; see
  `docs/PHONE_SETUP.md` for the tradeoff). Both are emailed to a Gmail
  "+" alias and read via IMAP using the same App Password as
  `send_email.py` - no Google Cloud project or service account.
- Retention defaults to 35 days and is controlled by `ARCHIVE_RETENTION_DAYS`.
  Portal and WhatsApp messages older than that window are removed from their
  JSON archives and saved input. Age uses the source message timestamp in IST,
  not the download date; messages exactly at the cutoff are retained. PDFs belonging only
  to expired messages are deleted, together with their extraction caches.
  A PDF shared with a retained message is kept. Unreferenced files under
  `data/` and old files under `output/` are removed after the retention window by modification
  time. Archives and the operational fetch cursor are retained as metadata.
  External portal/IMAP history is not deleted. Stored data remains available
  when a source fetch fails, with an explicit warning in the brief.
  Deleting a file doesn't reclaim git history size on its own though —
  the blob still exists in past commits until a history rewrite
  (`git gc`, BFG, `git filter-repo`), so repo size still grows slowly
  over time even with pruning in place, just far more slowly than
  without it.
