#!/bin/bash
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--check-evolution-port" ) ]]; then
    echo "Usage: $0 [--check-evolution-port]" >&2
    exit 2
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Native database startup is configured for this Mac. Use Docker on other hosts." >&2
    exit 1
fi
if [[ "${1:-}" == "--check-evolution-port" ]] && lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port 8080 is occupied. Stop the Docker stack or existing Evolution process before native debugging." >&2
    echo "Use Tasks: Run Task > Docker: stop stack, or run docker compose down." >&2
    exit 1
fi

postgres_bin="${NATIVE_POSTGRES_BIN:-$HOME/.local/share/mise/installs/postgres/14.9/bin}"
postgres_data="${NATIVE_POSTGRES_DATA:-$HOME/.local/share/postgres-neo}"
redis_cli="/opt/homebrew/opt/redis/bin/redis-cli"
redis_agent="$HOME/Library/LaunchAgents/sh.brew.redis.plist"
domain="gui/$(id -u)"
redis_job="$domain/sh.brew.redis"

for executable in "$postgres_bin/pg_ctl" "$postgres_bin/pg_isready" "$redis_cli"; do
    if [[ ! -x "$executable" ]]; then
        echo "Required native executable not found: $executable" >&2
        exit 1
    fi
done
if [[ ! -f "$postgres_data/PG_VERSION" || ! -f "$redis_agent" ]]; then
    echo "The preserved PostgreSQL data directory or Redis LaunchAgent is missing; no database was initialized." >&2
    exit 1
fi

if "$postgres_bin/pg_ctl" -D "$postgres_data" status; then
    echo "Reusing the running native PostgreSQL server."
else
    status=$?
    if [[ $status -ne 3 ]]; then
        echo "Could not inspect the native PostgreSQL server (exit $status)." >&2
        exit "$status"
    fi
    "$postgres_bin/pg_ctl" -D "$postgres_data" -l "$postgres_data/server.log" -w -t 60 start
fi
"$postgres_bin/pg_isready" -h 127.0.0.1 -p 5432 -d daily_school_evolution -t 5

if ! "$redis_cli" -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -qx PONG; then
    if launchctl print "$redis_job" >/dev/null 2>&1; then
        launchctl kickstart "$redis_job"
    else
        launchctl bootstrap "$domain" "$redis_agent"
    fi
fi
for ((attempt = 0; attempt < 30; attempt++)); do
    if "$redis_cli" -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -qx PONG; then
        echo "Native PostgreSQL and Redis are ready; Evolution can start."
        exit 0
    fi
    sleep 0.5
done
echo "Redis did not become ready on 127.0.0.1:6379. Check /opt/homebrew/var/log/redis.log." >&2
exit 1
