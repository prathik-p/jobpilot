#!/usr/bin/env bash
# Run JobPilot once. Used by cron. Logs to data/output/run.log
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null || true
python -m jobpilot run "$@" >> data/output/run.log 2>&1
