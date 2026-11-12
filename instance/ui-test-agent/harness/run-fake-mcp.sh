#!/bin/sh
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 jira|memory [additional fake_mcp_server.py arguments]" >&2
    exit 2
fi
KIND=$1
shift
case "$KIND" in
    jira|memory) ;;
    *) echo "Service must be jira or memory" >&2; exit 2 ;;
esac

exec uv run --project "$HARNESS_DIR" --extra servers \
  python "$HARNESS_DIR/fake_mcp_server.py" "$KIND" "$@"
