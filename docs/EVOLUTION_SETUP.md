# Direct WhatsApp ingestion with Evolution API

This source replaces phone-to-email forwarding. The brief service polls a
self-hosted Evolution API v2 instance for school-group messages and imports
text, captions, and PDF documents directly into the existing archive.
Evolution is the only supported WhatsApp source; email capture and brief
email delivery have been removed.

For the migrated four-container stack and switching back to native debugging,
see [Docker setup](DOCKER_SETUP.md). The native installation below remains
available as a separate development option; do not run both Evolution copies
against the same WhatsApp login at once.

## Account and privacy limitations

Evolution's `WHATSAPP-BAILEYS` connection is an unofficial WhatsApp Web client.
Account restrictions, bans, disconnects, and protocol breakage are possible,
including for read-only collection. Prefer a dedicated account that the school
allows into the group if practical. The account must already belong to the group.
The official WhatsApp Cloud API is not a replacement for access to an existing
personal account's school chats.

The linked session can access other chats even though this importer only saves
messages matching the exact configured group ID. Protect the API key, database,
Redis, session files, and local archives. Do not expose port 8080 publicly.
PDFs and school text subsequently enter the existing Azure processing pipeline.

History sync is best-effort, not a full backup. Expired media cannot always be
downloaded. Images, audio, video, stickers, and non-PDF document binaries are not
processed; image/video/document captions are imported. Revocations/deletions
are not propagated to the archive; retained messages expire by retention age.

## Native installation (no Docker)

The local installation prepared for this project is:

- Source: `../evolution-api`, pinned to upstream release `2.3.7`, with published
  Baileys upgraded to `7.0.0-rc14` and the local pairing compatibility patch below.
- Node.js and npm: the machine's existing native installation.
- PostgreSQL: existing local server; separate `daily_school_evolution` database
  and non-superuser login role. No existing databases were modified.
- Redis: existing local server, database 6, separate
  `daily_school_evolution` key prefix. No cache flush is required.
- Private configuration: `../evolution-api/.env`. The application's own `.env`
  has the matching API URL, key, and instance name. Do not commit either file.
- Native listener: `127.0.0.1:8080` only.

For a new machine, install Node.js 20+ (this installation was built with Node
24), PostgreSQL, Redis, and ffmpeg before proceeding. Then:

```bash
git clone --depth 1 --branch 2.3.7 \
  https://github.com/evolution-foundation/evolution-api.git ../evolution-api
cd ../evolution-api
npm ci --no-audit --no-fund
cp .env.example .env
chmod 600 .env
```

Create a dedicated database login and database using your PostgreSQL
administrator account. Do not reuse an unrelated application's database:

```bash
createuser -h 127.0.0.1 --pwprompt daily_school_evolution
createdb -h 127.0.0.1 --owner=daily_school_evolution daily_school_evolution
```

Configure Evolution's own `.env` with a freshly generated API key and the
database password. Percent-encode special characters in the database URL:

```dotenv
SERVER_HOST=127.0.0.1
SERVER_PORT=8080
SERVER_URL=http://127.0.0.1:8080
AUTHENTICATION_API_KEY=replace_with_a_random_secret
AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES=false
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=postgresql://daily_school_evolution:password@127.0.0.1:5432/daily_school_evolution?schema=evolution_api
DATABASE_CONNECTION_CLIENT_NAME=daily_school_brief
DATABASE_SAVE_DATA_INSTANCE=true
DATABASE_SAVE_DATA_NEW_MESSAGE=true
DATABASE_SAVE_DATA_HISTORIC=true
DATABASE_DELETE_MESSAGE=false
CACHE_REDIS_ENABLED=true
CACHE_REDIS_URI=redis://127.0.0.1:6379/6
CACHE_REDIS_PREFIX_KEY=daily_school_evolution
CACHE_REDIS_SAVE_INSTANCES=false
TELEMETRY_ENABLED=false
SENTRY_DSN=
LOG_LEVEL=ERROR,WARN,INFO,LOG
DEL_INSTANCE=false
CORS_ORIGIN=*
CORS_CREDENTIALS=false
```

Keep unused integrations disabled. Upstream 2.3.7's CORS middleware rejects
requests without an `Origin` header when using a restricted origin list, so
`CORS_ORIGIN=*` is needed for these server-to-server requests. It does not
replace API-key authentication or a private listener.

**Important native binding adjustment:** upstream 2.3.7 listens on all network
interfaces by default and does not read `SERVER_HOST`. In the separate
Evolution checkout's `src/main.ts`, replace its `server.listen(...)` call with:

```typescript
server.listen(httpServer.PORT, process.env.SERVER_HOST || '127.0.0.1', () =>
  logger.log(httpServer.TYPE.toUpperCase() + ' - ON: ' + httpServer.PORT),
);
```

This local adjustment is already applied to the prepared installation. Preserve
or reapply it when upgrading.

### Pairing compatibility patch (experimental)

The unmodified release bundles Baileys rc9. Besides being behind later security
fixes, it can fail to link a new device even when official WhatsApp Web works.
The upstream [pairing issue](https://github.com/WhiskeySockets/Baileys/issues/2737)
describes WhatsApp retiring a QR's registration secret through a
`companion_reg_refresh` notification. Published Baileys rc14 does not handle
that notification.

This installation uses a local JavaScript adaptation of
[Baileys PR #2765](https://github.com/WhiskeySockets/Baileys/pull/2765), revision
`4f263f0e365c2e74dd1b824031d1c5910f518c26`. That PR is **unmerged** at setup
time; this is experimental compatibility work, not an official stable fix or
an account-safety guarantee. No third-party fork is installed.

The adaptation preserves the current QR reference, rotates and persists the
32-byte advertisement secret on valid refresh notifications, and re-renders
the QR. Malformed notifications and already-registered sessions do not rotate
credentials. A version-specific patch is kept in this repository's
`patches/baileys+7.0.0-rc14.patch` and the native checkout's matching path.
The native checkout runs `patch-package` in `postinstall` so `npm ci` does not
silently lose the fix.

For a new installation, from the Evolution checkout:

```bash
npm install --save-exact baileys@7.0.0-rc14 --no-audit --no-fund
npm install --save-dev --save-exact patch-package@8.0.1 --no-audit --no-fund
mkdir -p patches
cp ../daily-school-brief/patches/baileys+7.0.0-rc14.patch patches/
npm pkg set scripts.postinstall=patch-package
npm run postinstall
```

Do not apply this patch to another Baileys version without reviewing its
compatibility. Remove the local adaptation only after verifying that the
upstream release includes the fix and passes the regression tests.
Build and launch from the Evolution checkout:

```bash
npm run db:generate
npm run db:deploy
npm run build
npm run start:prod
```

Keep that process running. Verify the readiness response and listener:

```bash
curl --fail http://127.0.0.1:8080/
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

The listener must say `127.0.0.1:8080`, not `*:8080`. Run one Evolution process.
Restart it with `npm run start:prod`; the database and Redis must be running
first. This setup does not install an automatic boot/login service. Stop it
with Ctrl-C when running in the foreground, or terminate only its known PID.
Do not kill shared PostgreSQL/Redis services or flush Redis.

### Start both applications from VS Code

In **Run and Debug**, select **Debug: service + Evolution** from the launch
dropdown and press F5. The compound configuration starts the Python service
and launches Node directly with the built `../evolution-api/dist/main.js`
entrypoint, equivalent to `npm run start:prod`. Evolution runs in the Debug
Console, with its working directory set to the sibling checkout, and reads its
own `.env` through its existing dotenv loader. This avoids shell activation
races and expanding private configuration into an integrated-terminal command.
Stopping either debug session stops
both. The existing Python-only launch and a separate **Debug: Evolution API**
launch are also available.

The Evolution launch runs **Native: prepare Evolution** to start/reuse native
PostgreSQL and Redis and wait for database readiness. Stop the Docker stack
first; an occupied port 8080 stops the task with an actionable error. Node
must be on VS Code's PATH, and Evolution must already be built.
When Evolution debugging stops, **Native: stop databases** stops PostgreSQL
and unloads Redis, including servers reused at startup. This also applies to
the combined launch. Database files and WhatsApp login state are preserved.
The compound starts both application processes
in parallel; it does not wait for Evolution readiness. Wait for its listener
before calling collection endpoints. Stop any separately running instance on
port 8080 before using the compound; if Evolution is already running and should
stay independent, select **Debug: service.py** instead.

## Pair WhatsApp and select the school group

Back in the **daily-school-brief** repository, configure its existing `.env`
without overwriting other credentials:

```dotenv
EVOLUTION_API_URL=http://127.0.0.1:8080
EVOLUTION_API_KEY=the_same_key_as_Evolution_AUTHENTICATION_API_KEY
EVOLUTION_INSTANCE=school
```

The helper reads credentials locally and never prints the API key:

```bash
# Only for a new instance; it is already created in the prepared installation.
.venv/bin/python evolution_admin.py create

.venv/bin/python evolution_admin.py pair
open output/evolution_qr.png
```

On the phone, open **WhatsApp > Linked devices > Link a device** and scan
the image. The QR is a login credential: do not share it or commit it.
If it expires, rerun `pair`. Instance creation sets `groupsIgnore=false`,
`syncFullHistory=true`, `readMessages=false`, `readStatus=false`, and
`alwaysOnline=false`. The importer does not send WhatsApp messages or mark
them as read.

When the Python service is running, authenticated `POST /whatsapp-login` can
return the PNG directly instead of writing a local QR file. This is suitable
for cloud deployments and does not require stopping the stack. Follow the
[pairing API contract](../README.md#whatsapp-qr-pairing-api), including pending
and already-connected responses. An explicit
`GET /health?check_connections=true` confirms UDT and WhatsApp connectivity;
default health responses show the last cached observation.

```bash
.venv/bin/python evolution_admin.py status
.venv/bin/python evolution_admin.py groups
```

Wait for group/history sync if necessary. Identify the school group by its
subject, then select its exact ID (not its display name):

```bash
.venv/bin/python evolution_admin.py select-group '120363000000000000@g.us'
```

The helper validates the ID against this instance's groups, then sets
`EVOLUTION_GROUP_JID` and `WHATSAPP_SOURCE=evolution` in the application's
`.env`. It also deletes the local QR image. Restart the brief service to load
the changed configuration. Email capture remains the default until selection;
there is no automatic fallback to a different source on Evolution failure.
No source selector is required. Remove a legacy `WHATSAPP_SOURCE=email` value
before restarting; if the variable is retained, it must be `evolution`.

## Verify collection

For ingestion only, without calling Azure or fetching the
school portal, stop the brief service and run:

```bash
.venv/bin/python - <<'PY'
import daily_brief

data = daily_brief.run(source="whatsapp")
source = data["whatsapp"]
if source["error"]:
    raise SystemExit(source["error"])
print(f"Retained WhatsApp messages: {len(source['messages'])}")
PY
```

Check `data/whatsapp_messages.json` and `data/pdfs/`. New Evolution records have
IDs prefixed `evolution:`. Once verified, start the brief service normally:

```bash
.venv/bin/python service.py
```

The existing scheduler, `/initialize`, `/fetch-whatsapp`, and `/refresh` now
use Evolution. Service collection may call Azure to transcribe new PDFs;
`/refresh` also generates the brief. Existing bearer-token requirements apply.
No inbound webhook, public tunnel, or capture mailbox is needed.

## Collection and recovery

- Both lower and upper timestamps are sent with each paginated message query.
  The group ID is checked again locally before text or media is imported.
- Normal polls only query messages strictly newer than the last successful
  WhatsApp collection start time stored in `data/last_run.json`. The query is
  bounded by retention and rounds its lower timestamp up to whole seconds,
  matching Evolution's timestamp precision. Messages at or before the cursor
  are not downloaded again.
- The first collection without a cursor uses the retention window. Explicit
  `/initialize` deliberately bypasses the cursor to recheck retained history;
  use it to recover late-synced messages dated before the last successful run.
- Stable instance/group/message IDs deduplicate replayed messages. Existing
  PDF files are reused; new PDFs are validated and saved atomically. PDFs over
  25 MiB fail explicitly rather than being silently omitted.
- Connection errors while querying messages, invalid API responses, invalid
  PDF content, and local storage failures fail collection. Prior archives and
  successful-fetch metadata remain intact.
- A failed PDF HTTP request does not block other messages: attachment metadata
  contains `download_error` instead of a nonexistent `saved_as` path. The
  service collection response and brief-generation input report the warning.
  Normal polls do not retry that older PDF. An explicit initialization can
  retry it; successful recovery replaces the warning with the saved-file
  reference. This covers expired historical media
  (WhatsApp HTTP 410, often surfaced by Evolution as HTTP 400) and transient
  network failures. No PDF contents are invented or silently dropped.
- Removing phone email capture does not remove retained email records.
  They do not share Evolution IDs, so overlapping old captures can remain
  duplicated until they expire. Disable phone capture after validating Evolution.
- Application retention only cleans the brief application's files, not
  Evolution's database/history, Redis, or sessions. Manage that storage and
  backups separately. Do not delete session data merely to prune history.
- Keep both processes and their dependencies available. A sleeping laptop
  does not collect on schedule. No automatic reconnect or ban-proof behavior
  is guaranteed; use `status` and refresh `pair` when relinking is necessary.

## Automated checks

These tests use simulated HTTP responses, never a real WhatsApp account:

```bash
.venv/bin/python -m pytest tests/test_fetch_evolution.py \
  tests/test_fetch_whatsapp.py tests/test_daily_brief.py tests/test_service.py -q
```

With the native installation present, run the pairing-patch regression tests
from the brief repository (set `EVOLUTION_HOME` for a different checkout path):

```bash
node --test tests/test_evolution_pairing.mjs
```

If iPhone reports "can't link new devices now", do not repeatedly scan fresh
codes or assume the account is banned. Check whether official WhatsApp Web
can link. Success there points toward the unofficial client's compatibility,
not a general device-linking restriction. Recheck upstream reports and the
local patch before another attempt. QR images are snapshots; rerun `pair`
immediately before scanning, because refresh notifications can invalidate
previously saved images.
