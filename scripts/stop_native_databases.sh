#!/bin/bash
set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "Usage: $0" >&2
    exit 2
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Native database shutdown is configured for this Mac. Use Docker on other hosts." >&2
    exit 1
fi

postgres_bin="${NATIVE_POSTGRES_BIN:-$HOME/.local/share/mise/installs/postgres/14.9/bin}"
postgres_data="${NATIVE_POSTGRES_DATA:-$HOME/.local/share/postgres-neo}"
redis_job="gui/$(id -u)/sh.brew.redis"
result=0

if [[ ! -x "$postgres_bin/pg_ctl" || ! -f "$postgres_data/PG_VERSION" ]]; then
    echo "The preserved PostgreSQL executable or data directory is missing." >&2
    result=1
elif "$postgres_bin/pg_ctl" -D "$postgres_data" status; then
    if ! "$postgres_bin/pg_ctl" -D "$postgres_data" -w -t 60 -m fast stop; then
        echo "Failed to stop native PostgreSQL." >&2
        result=1
    fi
else
    status=$?
    if [[ $status -eq 3 ]]; then
        echo "Native PostgreSQL is already stopped."
    else
        echo "Could not inspect native PostgreSQL (exit $status)." >&2
        result=1
    fi
fi

if launchctl print "$redis_job" >/dev/null 2>&1; then
    if ! launchctl bootout "$redis_job"; then
        echo "Failed to unload the native Redis LaunchAgent." >&2
        result=1
    fi
else
    echo "Native Redis LaunchAgent is already unloaded."
fi

for ((attempt = 0; attempt < 30; attempt++)); do
    if ! lsof -nP -iTCP:6379 -sTCP:LISTEN >/dev/null 2>&1; then
        exit "$result"
    fi
    sleep 0.5
done
echo "Port 6379 is still occupied. Redis may be running outside the configured LaunchAgent; it was not forcibly killed." >&2
exit 1
