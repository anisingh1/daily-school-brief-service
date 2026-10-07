# Daily School Brief

Pulls together messages from the school web portal and (via phone-forwarded
messages captured as email) the school WhatsApp group, and
produces a daily brief covering homework, tomorrow's agenda, tomorrow's
dress code, and other reminders using direct Azure OpenAI calls. Claude Code
and agent skills are not required to run the pipeline.

See `docs/superpowers/specs/2026-09-05-daily-school-brief-design.md` for the
original design and `docs/superpowers/plans/2026-09-05-daily-school-brief.md`
for the historical implementation plan. The current Azure workflow is below.

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
  rolling lookback window (standalone) or the orchestrated pipeline's
  month-start cutoff. PDF attachments are saved to `data/pdfs/`
  (prefixed `whatsapp_`); other attachment types are ignored.
- **`cutoff.py`** — `month_anchor()`: 2 days before the start of the
  current calendar month. Used directly for the WhatsApp fetch (its
  inbox already retains its own full history, so re-scanning the
  current month is enough), and as `portal_archive.py`'s fallback when
  no portal cursor has been recorded yet.
- **`portal_archive.py`** — persists the portal message archive
  (`data/portal_messages.json`, deduplicated by message id) and a
  "last run" cursor (`data/last_run.json`) in this git repo. The
  orchestrated pipeline fetches only what's new since the cursor (or
  since `month_anchor()` on a fresh setup), merges it into the archive,
  and always considers the FULL archive when generating the brief — so
  nothing scraped at any point in the past is ever dropped.
- **`daily_brief.py`** — orchestrator that calls both fetchers (portal
  via `portal_archive.py`'s cutoff, WhatsApp via `cutoff.month_anchor()`),
  treating each as best-effort (one source failing doesn't block the
  other), downloads portal attachments, and writes a combined JSON
  envelope to `output/daily_brief_input.json`. It then calls Azure generation,
  writes validated content, and renders/sends the email (or writes a preview).
  The `gather()` and `run()` Python functions still support collection alone.
- **`generate_brief.py`** - reads messages and extracts PDF text plus rendered
  page images, calls the configured Azure OpenAI deployment with the school
  rules, and validates the nine-field JSON output using Pydantic. Dates are
  computed in IST by Python. PDF images preserve scans and roster/table layout.
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

## Setup

1. Create a virtual environment (recommended, optional):
   ```
   python3 -m venv venv
   source venv/bin/activate   # on Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up your credentials:
   ```
   cp .env.example .env
   ```
   Then edit `.env` and fill in your real `UDT_USERNAME`, `UDT_PASSWORD`,
   desired `LOOKBACK_HOURS` (default 36, only used for standalone runs),
   `SMTP_USERNAME`/`SMTP_APP_PASSWORD`/`EMAIL_TO` for sending the brief
   by email (see `send_email.py`'s docstring for how to create a Gmail
   App Password), and — once the WhatsApp side is set up (see
   `docs/PHONE_SETUP.md`) — `WHATSAPP_EMAIL_TO`.
   **Never commit `.env`** - it's already in `.gitignore`.

4. Configure Azure OpenAI in `.env`:
  ```dotenv
  AZURE_OPENAI_ENDPOINT=https://gpt4-g11n.openai.azure.com
  AZURE_OPENAI_DEPLOYMENT=cont-aud-gpt-6-astra
  AZURE_OPENAI_API_VERSION=2025-04-01-preview
  AZURE_OPENAI_API_KEY=your_actual_key
  ```
  Deployment is the Azure deployment name, not the model family name. It
  must support Chat Completions, strict JSON-schema structured outputs, and
  image input for PDFs. The API key is loaded locally and never included in
  the prompt. Requests have a 120-second timeout and at most two SDK retries.
  School message text and rendered PDF pages are sent to your Azure endpoint.
  Review your organization's data-handling requirements before running.

## Run

- Portal scraper only (saves messages + downloads PDFs):
  ```
  python scrape_udt.py
  ```
- WhatsApp fetcher only (prints recent messages):
  ```
  python fetch_whatsapp.py
  ```
- Full pipeline (collects messages, generates the brief with Azure, and sends
  the HTML email):
  ```
  python daily_brief.py
  ```
- Full pipeline without sending email:
  ```
  python daily_brief.py --preview
  ```
- Regenerate from saved messages/PDFs without fetching or sending email:
  ```
  python daily_brief.py --from-input --preview
  ```
- Collection only (no Azure call or email):
  ```
  python daily_brief.py --gather-only
  ```
- Generate content only from existing input:
  ```
  python generate_brief.py
  ```
- For a scheduled environment that clones the repo fresh each run:
  ```
  python daily_brief.py --commit-archive
  ```
  This explicitly commits/pushes `data/` before generation. A local persistent
  installation retains its archive without this flag. Configure your scheduler
  to use the project's Python environment and working directory; no Claude
  scheduler is needed. Existing schedules are not changed automatically.

If collection, archive commit, or Azure generation fails, the pipeline replaces
old content with a warning-only brief and still attempts email/preview delivery.
It exits nonzero after delivery so the scheduler can detect the failure. Source
failures and unreadable PDFs appear as warnings; another source can still be
used. Invalid JSON, refusals, and truncated completions are never sent as a
normal brief. Empty sources skip the Azure call entirely. Oversized requests
produce a failure warning rather than silently discarding messages or pages.

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
- `data/portal_messages.json` — the permanent portal message archive,
  deduplicated by message id. Committed to this repo.
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
- Portal messages and their attachment PDFs older than 3 calendar months
  are pruned from `data/portal_messages.json` and deleted from
  `data/pdfs/` each run (see `portal_archive.py`'s `RETENTION_MONTHS`).
  Deleting a file doesn't reclaim git history size on its own though —
  the blob still exists in past commits until a history rewrite
  (`git gc`, BFG, `git filter-repo`), so repo size still grows slowly
  over time even with pruning in place, just far more slowly than
  without it.
