#!/bin/sh
# Container entry point for the scheduler service: prepares the database once, then runs
# the pipeline at the top of every hour. The app itself decides whether to send.
set -u

aid migrate && aid seed

while true; do
    now=$(date +%s)
    sleep $((3600 - now % 3600))
    aid run || echo "aid run exited with status $?"
done
