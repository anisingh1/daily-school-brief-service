# Docker and native development

## Start, inspect, and stop

Docker Desktop must be running. From the repository root:

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
docker compose logs --tail 100 brief evolution
docker compose down
```

VS Code also provides **Tasks: Run Task > Docker: start stack**, **Docker:
status**, and **Docker: stop stack**. The existing native debug launches are
unchanged. Stop native Python/Evolution before starting Docker, and stop the
Docker stack before starting the full native debug compound.

`down` removes this project's containers/network, not its named volumes.
**Do not use `down -v` or prune volumes:** that deletes the migrated database,
WhatsApp session, and application data. Restart policies restart containers
after failures or Docker engine startup, except after an intentional stop.
They do not start Docker Desktop or keep a sleeping Mac collecting messages.

## Ownership and persistence

Compose project `daily-school-brief` owns four services:

- `brief`: Python API and scheduler, `http://127.0.0.1:8000`.
- `evolution`: WhatsApp client/API, `http://127.0.0.1:8080`.
- `postgres`: dedicated PostgreSQL 14, accessible inside Docker only.
- `redis`: dedicated Redis 7, accessible inside Docker only.

Evolution connects to the database/cache; it does not launch them. Compose
waits for PostgreSQL/Redis health, then Evolution HTTP readiness, then starts
Python. HTTP readiness does not itself guarantee WhatsApp is linked:

```bash
docker compose exec brief python evolution_admin.py status
```

For cloud relinking, the Python API also provides authenticated
`POST /whatsapp-login`, returning the PNG directly. There is no need to expose
Evolution publicly or download a file from its volume. Use the Docker API token
(`DOCKER_SERVICE_API_TOKEN`) and inspect HTTP status before displaying the
response: 202 means retry shortly, and 409 can mean already connected or busy.
`GET /health?check_connections=true` actively verifies UDT and WhatsApp;
ordinary `/health` only returns cached observations, so Docker's frequent
health probes do not repeatedly log in to UDT. See the
[API details](../README.md#whatsapp-qr-pairing-api).

Named volumes hold the PostgreSQL database, Redis persistence, Evolution
instances/store, and Python data/output. Inspect their ownership with:

```bash
docker volume ls --filter label=com.docker.compose.project=daily-school-brief
docker compose ps
```

The containers run independently of VS Code. Python and Evolution run as
unprivileged users. PostgreSQL/Redis ports are not published; Python's
container bind is `0.0.0.0`, but host access is limited to localhost.
All container logs are rotated, capped at three 10 MB files per service.

The Evolution image builds from upstream revision
`cd800f2976e1e5b682fbf86a01ee4d85ae61f370` (2.3.7), upgrades Baileys to rc14,
and applies this repository's pairing patch. Its six regression tests must
pass during the build. It does not require the sibling native checkout.
Prisma migrations run before Node starts; the entrypoint does not print the
database password/connection URL.

## Configuration

Both modes use the private, ignored root `.env`. Existing native values remain
unchanged. Add two Docker-only settings from `.env.example`:

```dotenv
DOCKER_POSTGRES_PASSWORD=replace_with_random_hex
DOCKER_SERVICE_API_TOKEN=replace_with_random_hex
```

Generate each value with `openssl rand -hex 32`. Use URL-safe hex for the
database password because it is embedded in Evolution's connection URI.
Keep `.env` private; `docker compose config` without `--quiet` prints secrets.
Never commit or share it or Docker inspection output containing environment
variables. Changing a password after volume initialization also requires
changing the database role password; changing `.env` alone is insufficient.

Compose overrides only container-specific settings: service bind/reload,
Evolution's internal address, and the API token. Native `SERVICE_API_TOKEN`
is unchanged; Docker HTTP clients use `DOCKER_SERVICE_API_TOKEN`. The usual
bearer token is required for all data/control routes, including health and PDF
downloads. Missing server-token configuration fails closed with HTTP 503,
including native debugging and direct ASGI launches. Frontends must fetch PDF
links with an authorization header; plain unauthenticated links no longer work.
Azure/portal/WhatsApp settings remain required. No `.env`, school data, or
native session is copied into either image.

Set `AZURE_OPENAI_PDF_DEPLOYMENT` and `AZURE_OPENAI_BRIEF_DEPLOYMENT` in `.env`
to use separate Azure deployments for PDF extraction and brief generation.
Both use the same endpoint, API key, and API version. Blank task settings use
the shared `AZURE_OPENAI_DEPLOYMENT` fallback. Compose passes these settings
through without overrides; recreate `brief` after changing them.

For a fresh installation, configure `.env`, run the startup command, then
create/pair/select the WhatsApp instance with `docker compose exec brief python
evolution_admin.py ...`. Initialize and refresh through the authenticated API.
Existing migrated installations do not need to create or pair again.

## Existing migration

The performed migration stopped the project's native Python/Evolution
processes, stopped the native PostgreSQL server, and unloaded the active
`sh.brew.redis` LaunchAgent. It did not delete native databases, Redis files,
application files, launch definitions, or the sibling Evolution checkout.
Unrelated containers were not stopped.

The dedicated Evolution database was exported privately to
`.docker-data/native-evolution.dump` and restored into the Docker PostgreSQL
volume. Redis was started with a fresh dedicated cache; in this setup it is
not the authoritative WhatsApp session store. The native instances directory
was empty; the database preserved the working login.

The Python data/output were staged under `.docker-data/app` with:

```bash
.venv/bin/python docker/migrate_local_data.py --destination .docker-data/app
```

This script never modifies the originals and refuses an existing destination.
It validates JSON and symlinks before copying and converts absolute native PDF
references to `/app/data/pdfs/...`. It retains messages, cursors, attendance,
PDF bytes, transcriptions, and the latest brief. Only the copied generation
fingerprint is removed: container paths are different, so the first scheduled
refresh must establish a new fingerprint.

The initial database restore and file import are **one-time migration steps**,
not part of normal startup. Do not repeat them over a working Docker database.
The file import used a one-off root container to populate the fresh data/output
volumes, then assigned ownership to the Python service user (UID/GID 10001).
The original snapshots remain private and ignored by Git. Treat the database
dump as a login credential.

Docker and native storage are separate copies. Later Docker collections and
session updates are not automatically written back to the native snapshot.
For debugging against the latest live state, prefer the hybrid mode below;
do not run two Evolution copies of the same WhatsApp login simultaneously.

## Native debugging remains available

### Recommended: native Python with Docker dependencies

Keep the same live Evolution session/database and debug Python locally:

```bash
docker compose up -d postgres redis evolution
docker compose stop brief
```

Select **Debug: service.py** in VS Code. Its existing native `.env` points to
Evolution on localhost:8080. Python uses the original native data/output, not
the Docker volumes. Stop the Python debug session, then run `docker compose
up -d --wait` to resume the Docker Python service.

### Fully native Python and Evolution

First run `docker compose down`, then select **Debug: service + Evolution**.
Both this compound and the Evolution-only launch now run **Native: prepare
Evolution** before starting Node. The task checks that port 8080 is free,
starts/reuses the preserved PostgreSQL server, starts/reuses the Redis
LaunchAgent, and waits for both databases to be ready. It fails explicitly
rather than starting another Evolution when Docker already owns the port.

To start just the native databases, use **Tasks: Run Task > Native: start
databases**, or:

```bash
bash scripts/start_native_databases.sh
```

The helper is configured for this Mac's existing mise PostgreSQL 14.9 and
Homebrew Redis installations, not required for Docker. Set
`NATIVE_POSTGRES_BIN` and `NATIVE_POSTGRES_DATA` in the task environment if those
PostgreSQL paths change. It never initializes a new database, flushes Redis,
or stops existing services. Repeated runs reuse ready servers.
When the Evolution debug session ends (including through the combined launch),
its **Native: stop databases** post-debug task gracefully stops PostgreSQL and
unloads the Redis LaunchAgent so it cannot immediately restart Redis.
This also stops servers that were already running before debugging; avoid
sharing these native databases with other projects during a debug session.
The stop task is safe to repeat and can also be run manually after using
**Native: start databases**. It does not delete data or affect Docker containers.
The Redis job was unloaded, not disabled: it may
also load again at the next user login. Neither native database port conflicts
with Docker because the container databases are private.

The sibling Evolution checkout, its local patches/configuration, original
PostgreSQL database, Python virtual environment, and all native debug launch
entries are retained. Full native Evolution uses the pre-migration session
snapshot, not subsequent Docker changes. Check `evolution_admin.py status`
after switching; a stale/revoked login may need relinking.

Stop the native debug sessions before returning to Docker. The post-debug task
stops the native databases; they are not Docker dependencies.
