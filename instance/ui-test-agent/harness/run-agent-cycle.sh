#!/bin/sh
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec uv run --project "$HARNESS_DIR" python \
  "$HARNESS_DIR/run_agent_cycle.py" "$@"
