#!/bin/sh
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec uv run --project "$HARNESS_DIR" python -m unittest discover \
  -s "$HARNESS_DIR/tests" -v
