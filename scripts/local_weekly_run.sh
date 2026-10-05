#!/usr/bin/env bash
# Weekly local run: ingest -> enrich -> silver -> mart -> monitor -> inspect.
# See docs/runbook-local-monitoring.md for what each step should print.
# Stops at the first failing step; a monitor history barrier exits 1 on purpose.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs
log="logs/weekly-$(date +%Y%m%d-%H%M%S).log"

run() {
  echo "==> $*" | tee -a "$log"
  "$@" 2>&1 | tee -a "$log"
}

run uv run registry ingest-fda-list -v
run uv run registry enrich-openfda -v
run uv run registry build-silver -v
run uv run registry build-mart -v
run uv run registry monitor -v
run uv run registry inspect

echo "Done. Log: $log"
