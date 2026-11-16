#!/bin/sh
# Container entry point for the scheduler service. Setup must succeed: if migrating or
# seeding fails the script exits, the container restarts, and the failure is visible in
# `docker compose ps` instead of a loop running against an old schema.
set -eu

aid migrate
aid seed

# `docker stop` signals this shell, not the command it is waiting for. Every command
# therefore runs in the background and the signal is passed on, so a run that is
# stopped half way records itself as cancelled and releases its lease.
child=""

stop() {
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" || true
    fi

    exit 143
}

trap stop TERM INT

# A failed hour is logged and the loop carries on; the next hour retries.
attempt() {
    "$@" &
    child=$!
    wait "$child" || echo "$* exited with status $?"
    child=""
}

while true; do
    now=$(date +%s)
    attempt sleep $((3600 - now % 3600))
    attempt aid maintain
    attempt aid run
done
