#!/bin/sh
# Container entry point for the scheduler service. Setup must succeed: if migrating or
# seeding fails the script exits, the container restarts, and the failure is visible in
# `docker compose ps` instead of a loop running against an old schema.
set -eu

aid migrate
aid seed

while true; do
    now=$(date +%s)
    sleep $((3600 - now % 3600))
    # A failed hour is logged and the loop carries on; the next hour retries.
    aid maintain || echo "aid maintain exited with status $?"
    aid run || echo "aid run exited with status $?"
done
