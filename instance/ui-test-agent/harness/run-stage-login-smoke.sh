#!/bin/sh
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOGIN_HELPER_DIR=$(CDPATH= cd -- "$HARNESS_DIR/../agent/login-helper" && pwd)
NODE=$(command -v node || true)
if [ -z "$NODE" ]; then
  echo "Node.js is required. Install Node.js 22 or newer." >&2
  exit 1
fi
if [ -z "${UI_TEST_USERNAME:-}" ] || [ -z "${UI_TEST_PASSWORD:-}" ]; then
  echo "Set UI_TEST_USERNAME and UI_TEST_PASSWORD (for example through op run)." >&2
  exit 1
fi

CHROME_BIN=${CHROME_BIN:-}
if [ -z "$CHROME_BIN" ]; then
  for candidate in chromium chromium-browser google-chrome; do
    CHROME_BIN=$(command -v "$candidate" || true)
    [ -n "$CHROME_BIN" ] && break
  done
fi
if [ -z "$CHROME_BIN" ] && [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi
if [ -n "$CHROME_BIN" ] && [ ! -x "$CHROME_BIN" ]; then
  CHROME_BIN=$(command -v "$CHROME_BIN" || true)
fi
if [ -z "$CHROME_BIN" ] || [ ! -x "$CHROME_BIN" ]; then
  echo "Could not find Chromium. Set CHROME_BIN to its executable path." >&2
  exit 1
fi

if ! (cd "$LOGIN_HELPER_DIR" && "$NODE" --input-type=module -e \
  "await import('playwright'); await import('@redhat-cloud-services/playwright-test-auth')"); then
  echo "Stage login dependencies are missing. Run: npm ci --prefix instance/ui-test-agent/agent/login-helper" >&2
  exit 1
fi

if "$NODE" -e '
  const socket = require("node:net").connect(9222, "127.0.0.1");
  socket.once("connect", () => { socket.destroy(); process.exit(0); });
  socket.once("error", () => process.exit(1));
'; then
  echo "Port 9222 is already in use. Stop the other CDP browser and retry." >&2
  exit 1
fi

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ui-test-stage-smoke.XXXXXX")
CHROME_PID=
cleanup() {
  if [ -n "$CHROME_PID" ] && kill -0 "$CHROME_PID" 2>/dev/null; then
    kill "$CHROME_PID" 2>/dev/null || true
    wait "$CHROME_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Starting isolated Chromium for https://console.stage.redhat.com."
"$CHROME_BIN" \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --no-first-run \
  --disable-sync \
  --disable-extensions \
  --use-mock-keychain \
  --disable-background-networking \
  --proxy-server=http://squid.corp.redhat.com:3128 \
  '--proxy-bypass-list=localhost;127.0.0.1;::1' \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  "--user-data-dir=$WORK_DIR/chromium-profile" \
  https://console.stage.redhat.com >"$WORK_DIR/chromium.log" 2>&1 &
CHROME_PID=$!

if ! "$NODE" --input-type=module -e '
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch("http://127.0.0.1:9222/json/version");
      if (response.ok) process.exit(0);
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  process.exit(1);
'; then
  echo "Chromium did not start its CDP endpoint. Browser log:" >&2
  cat "$WORK_DIR/chromium.log" >&2
  exit 1
fi

echo "Running the Stage sign-in and signed-in marker check."
if RESULT=$(UI_TEST_TARGET_URL=https://console.stage.redhat.com "$NODE" \
  "$LOGIN_HELPER_DIR/browser-login.mjs"); then
  printf '%s\n' "$RESULT"
else
  echo "Stage login helper failed." >&2
  exit 1
fi

STATUS=$(printf '%s' "$RESULT" | "$NODE" -e '
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => { input += chunk; });
  process.stdin.on("end", () => {
    try { process.stdout.write(JSON.parse(input).status || "unknown"); }
    catch { process.stdout.write("invalid-result"); }
  });
')
case "$STATUS" in
  authenticated|already_authenticated)
    echo "PASS: signed-in User Avatar marker observed ($STATUS)."
    ;;
  *)
    echo "FAIL: Stage sign-in was not confirmed ($STATUS)." >&2
    exit 1
    ;;
esac
