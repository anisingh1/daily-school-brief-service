# Daily School Brief — Always-On Service — Design

## Goal

Move off the current model (a Claude Code skill triggered once daily by a
scheduled cloud routine, emailing an HTML brief) to an always-on service
running on the user's own Raspberry Pi / home server, which:

- Polls the WhatsApp source every 10 minutes and the school portal every 60
  minutes, regenerating the brief only when something new is found.
- Also forces one regeneration per day (~7:30pm IST, matching today's
  schedule) as a backstop, so date-relative fields (tomorrow's dress code,
  which day's classwork carries forward) stay correct even across a gap
  with no new messages (e.g. a weekend).
- Serves the current brief on a simple web page on the home LAN, instead
  of sending it by email.

This lives **alongside** the existing scheduled-cloud-agent + email setup,
not in place of it: all new code goes into a new `pi_service/` subfolder,
so the existing root-level scripts, the `.claude/skills/daily-school-brief/`
skill, and the scheduled cloud routine that calls it keep working exactly
as they do today, untouched. The two systems run independently side by
side; the user can retire the old one manually later once the Pi service
is proven out, but that's a separate future decision, not part of this
work.

The design still supersedes the *approach* described in
`docs/superpowers/specs/2026-09-05-daily-school-brief-design.md` (scheduled
cloud agent + email delivery) for the new folder — the fetcher *logic*
(`scrape_udt.py`, `fetch_whatsapp.py`, `cutoff.py`, the portal
archive/cursor pattern) is carried over conceptually and adapted, but as
independent copies inside `pi_service/`, not shared modules — see "Why not
X" for why.

## Why not X

- **Cloud-scheduled agent run every 10 min**: rejected. Cloud routines are
  designed for periodic scheduled runs, not tight polling, and each run
  would cost an LLM invocation whether or not anything new existed. A
  persistent process that only calls the LLM when there's new content (or
  once/day as a backstop) is both cheaper and a better fit for "check
  every 10 min."
- **Managed cloud VM/container (Fly.io, Railway, etc.)**: not needed — the
  user already has a Raspberry Pi / home server running 24/7.
- **Polling the school portal every 10 min (same cadence as WhatsApp)**:
  rejected. ~144 logins/day vs. today's ~1/day meaningfully raises the risk
  of the portal treating this as abuse or the account getting rate-limited/
  locked. WhatsApp polling (IMAP against the user's own Gmail) doesn't carry
  that risk, so it keeps the tighter 10-minute cadence while the portal
  polls hourly instead.
- **Public/internet-exposed web page**: rejected — only needs to be viewed
  from home Wi-Fi, so there's no need for a reverse proxy, dynamic DNS,
  tunnel, or authentication. LAN-only keeps this simple.
- **git-committed archive as the persistence mechanism**: this existed
  specifically because the old scheduled cloud agent cloned the repo fresh
  on every run — anything not pushed back to GitHub before the run ended
  was lost. A persistent Pi process doesn't have that problem; its local
  disk *is* the persistence layer, so the git-commit-and-push step
  (`commit_archive.py`) is not used in the new folder's runtime path.
- **Modifying the existing root-level scripts in place** (e.g. adding a
  cursor to the existing `fetch_whatsapp.py`, changing `daily_brief.py` to
  call the new generator): rejected per explicit instruction — the
  existing scheduled cloud routine + email flow must keep working
  unmodified. Independent copies inside `pi_service/` avoid any risk of a
  change made for the new service accidentally breaking the old one (or
  vice versa), at the cost of some duplicated fetcher logic between the
  two folders.

## Architecture

```
repo root (unchanged)                    pi_service/ (new, self-contained)
├── scrape_udt.py         still used     ├── scrape_udt.py        (copy, unmodified)
├── fetch_whatsapp.py     by the old     ├── fetch_whatsapp.py    (copy, unmodified)
├── cutoff.py             scheduled      ├── cutoff.py             (copy, unmodified)
├── portal_archive.py     cloud routine  ├── portal_archive.py     (copy, import fixed)
├── daily_brief.py        + email, as    ├── service.py           (new)
├── send_email.py         today          ├── generate_brief.py    (new)
├── render_email.py                      ├── render_brief.py      (new, adapted
├── commit_archive.py                    │                         from render_email.py)
├── .claude/skills/...                   ├── data/, output/       (own, separate
├── data/, output/                       │                         from root's)
└── .env                                 └── .env                 (own)

[School Portal]                          [Gmail inbox]
      │                                        │
      │ pi_service/scrape_udt.py,              │ pi_service/fetch_whatsapp.py,
      │ portal_archive cursor                  │ full 3-month refetch each time,
      │ (incremental fetch)                    │ new-content detected by diffing
      │ polled every 60 min                    │ polled every 10 min
      │                                        │
      └───────────────┬────────────────────────┘
                       ▼
         ┌───────────────────────────────────┐
         │ pi_service/service.py              │
         │ (systemd, always on)               │
         │                                     │
         │  scheduler:                         │
         │   - whatsapp loop (10 min)          │
         │   - portal loop (60 min)            │
         │   - daily backstop (~7:30pm IST)     │
         │        │ (new content found, or      │
         │        │  backstop fires)             │
         │        ▼                              │
         │   regenerate_brief() [locked]         │
         │     → generate_brief.py               │
         │       (Anthropic API call, same       │
         │        categorization rules as        │
         │        today's SKILL.md + any new     │
         │        PDFs as document blocks)       │
         │     → writes output/daily_brief_      │
         │       content.json + rendered HTML    │
         │                                        │
         │  web server (Flask):                   │
         │     serves latest rendered brief at    │
         │     http://<pi-hostname>.local:8765/   │
         └───────────────────────────────────────┘
                       ▲
                       │ LAN only, no auth
                  [Browser at home]
```

## Components

All of the following live under a new `pi_service/` folder at the repo
root, with their own `data/`, `output/`, and `.env` — entirely separate
from the root-level `data/`, `output/`, and `.env` the existing scheduled
routine uses. Nothing under `pi_service/` imports from or writes to the
root-level scripts or their state, and nothing at the root imports from
`pi_service/`.

### 1. Portal fetcher — `pi_service/scrape_udt.py`, `pi_service/portal_archive.py` (copied)

`scrape_udt.py` is copied byte-for-byte, no changes. `portal_archive.py`
is copied with one required one-line fix: its `from cutoff import
month_anchor` is a bare, unqualified import, which would otherwise
resolve to whichever `cutoff` module sys.path finds first (risking a
silent dependency on the *root's* `cutoff.py` instead of `pi_service`'s
own copy, defeating the point of duplicating it). Changed to `from
pi_service.cutoff import month_anchor`. Everything else is unchanged:
logs in, scrapes, dedups by message id into
`pi_service/data/portal_messages.json`, tracks a cursor in
`pi_service/data/last_run.json`. Invoked once per 60-minute poll. The
2-month pruning behavior is unchanged.

### 2. WhatsApp fetcher — `pi_service/fetch_whatsapp.py` (byte-for-byte copy, unmodified)

Copied from the root-level version with **no changes at all** — every
path it touches is already derived from `Path(__file__).parent`, so the
copy automatically reads/writes under `pi_service/data/` without edits.

No cursor is added here. A cursor would only speed up the *fetch*, but
the *brief* still needs the full relevant history every time it
regenerates (e.g. to carry forward homework from a few days ago) — the
same problem the portal's archive was built to solve. Unlike the portal,
there's no login-cost reason to make the WhatsApp fetch incremental: it's
an IMAP `SEARCH` against the user's own Gmail inbox, which comfortably
handles being called every 10 minutes at full-window cost. So every poll
just calls `fetch_recent_whatsapp_messages(cutoff=month_anchor(months_back=3))`
— identical to what `daily_brief.py` already does once a day — and
"is anything new" is detected separately (see component 3) by comparing
that poll's fetched result to the previous poll's, not by a persisted
cursor.

### 3. `pi_service/service.py` (new) — the always-on entrypoint

Starts three scheduled loops (WhatsApp/10 min, portal/60 min, daily
backstop/~7:30pm IST) and the web server, all in one process. Holds a
lock around brief regeneration so two triggers (e.g. a WhatsApp poll and
the daily backstop) firing close together can't race. Scheduling is a
plain background loop per task (a thread per interval, each sleeping until
its next run time) — no extra scheduling dependency needed for three fixed
intervals, and it matches the rest of the codebase's plain-synchronous
style (no existing script uses `asyncio` or a scheduling library).

Since the WhatsApp fetch has no persisted cursor (see component 2),
`service.py` keeps the previous poll's fetched WhatsApp message list
in memory (module-level state, reset on service restart) and compares
each new poll's result against it (by message identity — timestamp +
sender + text) to decide whether anything changed. A restart re-fetches
and treats the first post-restart poll as a fresh baseline (no
regeneration fires on that comparison alone) — acceptable since the
daily backstop still guarantees a regeneration within 24 hours even if a
restart happens to swallow one change notification.

### 4. `pi_service/generate_brief.py` (new) — replaces the Claude Code skill

Calls the Anthropic API directly (Python SDK, `ANTHROPIC_API_KEY` from
`pi_service/.env`) with the same categorization instructions currently
embedded in `.claude/skills/daily-school-brief/SKILL.md` steps 4–8 (Aviraj
callouts, per-student roster checks, carry-forward classwork/homework,
fixed swim/skate schedule with override rule, 5-day reminder window),
built from an input envelope assembled the same way `daily_brief.py` does
today (that envelope-building logic is copied into `pi_service/` too,
rather than importing the root-level `daily_brief.py`). Portal/WhatsApp
PDF attachments are sent as document content blocks in the API call
(replacing the skill's `Read`-the-PDF-directly step). Returns the same
JSON shape as today: `date`, `warnings`, `aviraj_highlight`, `classwork`,
`homework`, `agenda`, `dress_code`, `reminders`.

### 5. `pi_service/render_brief.py` (new, adapted from `render_email.py`)

Copied from the root-level `render_email.py` and adapted to produce the
HTML shown on the web page instead of an email body. The root-level
`render_email.py` (still used for the email flow) is untouched.

### 6. Web page (new, Flask)

Single route, `/`, rendering the latest
`pi_service/output/daily_brief_content.json` via `render_brief.py`. Flask
(run via its own built-in server, threaded) is used rather than an async
framework, to match the rest of the codebase's plain-synchronous style and
because a single-route, single-user, LAN-only page has no need for async
concurrency. Reachable at `http://<pi-hostname>.local:8765/` on the home
LAN (port configurable via `pi_service/.env`'s `PORT`, default `8765`). No
authentication, no external exposure. If the last regeneration attempt
failed, the last successfully-generated brief is still shown, with a small
note (e.g. "last updated <time>; last attempt at <time> failed: <reason>")
rather than showing nothing.

### 7. Existing code: unchanged

Nothing at the repo root is modified, removed, or renamed:
`send_email.py`, `render_email.py`, `commit_archive.py`, `daily_brief.py`,
`scrape_udt.py`, `fetch_whatsapp.py`, `cutoff.py`, `portal_archive.py`,
`.claude/skills/daily-school-brief/SKILL.md`, the root `data/`/`output/`/
`.env`, and the scheduled cloud routine that runs the skill all keep
working exactly as they do today. `pi_service/` is purely additive.

### 8. `pi_service/.env` (new, separate from the root `.env`)

- `UDT_USERNAME`, `UDT_PASSWORD` — portal credentials (same values as the
  root `.env`, copied over; not shared/read from the root file).
- IMAP/Gmail credentials for the WhatsApp source (same values as the root
  `.env`, copied over).
- `ANTHROPIC_API_KEY` — for direct API calls from `generate_brief.py`.
- `PORT` — port for the Flask web page, default `8765`.

### 9. Deployment

A systemd unit (e.g. `daily-school-brief-pi-service.service`) runs
`pi_service/service.py` on boot and restarts it on failure, so the service
survives Pi reboots without manual intervention. Named distinctly from the
existing flow so it's clearly a separate unit if the old scheduled routine
is ever also run as a local service in the future.

## Data flow / trigger logic

- **WhatsApp loop (10 min)**: fetch the full 3-month window → compare
  against the previous poll's fetched result → if different, trigger
  `regenerate_brief()` (passing this poll's full fetched list, so the
  brief always has complete context, not just what's new). If unchanged,
  no-op (no LLM call).
- **Portal loop (60 min)**: fetch since the portal cursor → if the merge
  into the archive adds anything new, trigger `regenerate_brief()`.
- **Daily backstop (~7:30pm IST, once/day)**: always triggers
  `regenerate_brief()`, regardless of whether anything new arrived, so
  "tomorrow"-relative fields stay correct across gaps in new messages.
- **`regenerate_brief()`**: acquires the lock, builds the input envelope,
  calls the Anthropic API, writes `pi_service/output/daily_brief_content.json`
  and the rendered HTML, releases the lock.

## Error handling

- Best-effort per source, same as today: a portal or WhatsApp fetch
  failure surfaces as a warning in the next generated brief rather than
  blocking the other source.
- If `regenerate_brief()`'s LLM call itself fails, the web page keeps
  showing the last successfully-generated brief, with a note about the
  last failed attempt and its reason, rather than going blank or crashing
  the service.
- No retries or alerting beyond this — personal tool, not a monitored
  service, consistent with the existing design's scope.

## Testing approach

- Unit tests for the WhatsApp new-content diffing logic and for
  `generate_brief.py`'s input-envelope/prompt construction, mocking the
  Anthropic API call — following the existing patterns in `tests/`.
- Manual verification on the actual Pi: systemd service starts on boot,
  survives a reboot, and the web page is reachable from another device on
  the home LAN.
- One live end-to-end dry run (real portal login, real IMAP fetch, real
  Anthropic API call) before leaving the service running unattended,
  checked by hand against expected output.
- No load/scale testing — single-user, LAN-only, personal tool.

## Scope / out of scope

- Single child, single class WhatsApp group, single portal account —
  unchanged from the existing design.
- LAN-only web access — no external exposure, no authentication, no
  multi-user support.
- No brief history/archive page — the web page shows only the current/
  latest brief, matching how the tool is used today (one brief at a time).
- No database — local JSON files on the Pi's disk under `pi_service/`,
  mirroring today's shapes (`portal_messages.json`, `last_run.json`,
  `daily_brief_content.json`), but stored separately from the root-level
  copies. WhatsApp has no on-disk archive or cursor, matching today's
  design (its fetch is always a stateless full-window re-scan).
- No changes to the existing scheduled-cloud-agent + email flow, and no
  migration/cutover step — retiring it (if the user chooses to, once the
  Pi service is proven out) is explicitly out of scope for this work.
