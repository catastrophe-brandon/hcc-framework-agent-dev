"""Alias-based loopback HTTP bridge for the Stage Chromium session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import TypedDict
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

TARGET_URL = "https://console.stage.redhat.com"
LOGIN_SCRIPT = Path(__file__).with_name("browser-login.mjs")
PORT = int(os.environ.get("UI_TEST_LOGIN_HELPER_PORT", "8768"))
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
BROWSER_LOGIN_TIMEOUT_SECONDS = 240
PROFILE_DIR = Path("/tmp/ui-test-chromium")
password_submission_at: float | None = None
PASSWORD_RETRY_COOLDOWN_SECONDS = 300
login_lock = threading.Lock()
CREDENTIALS_DIR = Path(
    os.environ.pop("UI_TEST_CREDENTIALS_DIR", "") or "/var/run/ui-test-credentials"
)
HARNESS_PROFILE = os.environ.pop("UI_HARNESS_PROFILE", "")
HARNESS_USERNAME = os.environ.pop("UI_TEST_USERNAME", "")
HARNESS_PASSWORD = os.environ.pop("UI_TEST_PASSWORD", "")
HARNESS_SHOW_COOKIE_PROMPT = os.environ.pop("UI_HARNESS_SHOW_COOKIE_PROMPT", "") == "1"
ALIAS_PATTERN = re.compile(r"[A-Za-z0-9:_-]{1,80}\Z")
PUBLIC_MESSAGES = {
    "blocked": "The login helper could not prepare or access Stage sign-in.",
    "sign_in_form_not_found": (
        "The standard Red Hat SSO login form did not appear. Report the sign-in "
        "blocker."
    ),
    "login_unconfirmed": (
        "The SSO helper could not confirm Stage sign-in. Do not assume "
        "authentication or retry."
    ),
    "authentication_failed": (
        "Red Hat SSO rejected the configured test account. Do not retry."
    ),
    "authenticated": (
        "Stage loaded and the signed-in User Avatar control is visible. Verify "
        "the account and org context."
    ),
}
SIGN_IN_INCOMPLETE = {
    "status": "blocked",
    "message": "The login helper could not complete the Stage sign-in flow.",
    "credential_submission_started": True,
}


class LoginResult(TypedDict, total=False):
    status: str
    message: str
    profile_alias: str
    credential_submission_started: bool


attempts: deque[LoginResult] = deque(maxlen=100)


def _credentials_for(alias: str) -> tuple[str, str] | None:
    """Resolve a profile inside the helper, never from a request credential."""
    if not ALIAS_PATTERN.fullmatch(alias):
        return None

    if HARNESS_PROFILE:
        if HARNESS_USERNAME and HARNESS_PASSWORD and alias == HARNESS_PROFILE:
            return HARNESS_USERNAME, HARNESS_PASSWORD
        return None

    try:
        username = (
            (CREDENTIALS_DIR / f"{alias}.username")
            .read_text(encoding="utf-8")
            .rstrip("\r\n")
        )
        password = (
            (CREDENTIALS_DIR / f"{alias}.password")
            .read_text(encoding="utf-8")
            .rstrip("\r\n")
        )
    except OSError:
        return None
    if not username or not password:
        return None
    return username, password


def _public_login_result(payload: object) -> LoginResult:
    """Allow only known statuses and fixed messages from the browser process."""
    if not isinstance(payload, dict):
        return {"status": "blocked", "message": PUBLIC_MESSAGES["blocked"]}

    status = payload.get("status")
    if not isinstance(status, str) or status not in PUBLIC_MESSAGES:
        return {"status": "blocked", "message": PUBLIC_MESSAGES["blocked"]}

    return {
        "status": status,
        "message": PUBLIC_MESSAGES[status],
        "credential_submission_started": bool(
            payload.get("credential_submission_started", False)
        ),
    }


async def health(_request: Request) -> JSONResponse:
    if await asyncio.to_thread(_chromium_cdp_ready):
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "browser_unavailable"}, status_code=503)


async def harness_state(_request: Request) -> JSONResponse:
    return JSONResponse({"attempts": list(attempts)})


async def sign_in_endpoint(request: Request) -> JSONResponse:
    """Accept only a configured profile alias; never accept credential fields."""
    try:
        body = await request.body()
        if len(body) > 256:
            raise ValueError("request body too large")
        payload = json.loads(body)
    except ValueError:
        return JSONResponse(
            {"status": "blocked", "message": "Request must contain one profile alias."},
            status_code=400,
        )

    if (
        not isinstance(payload, dict)
        or set(payload) != {"alias"}
        or not isinstance(payload["alias"], str)
    ):
        return JSONResponse(
            {"status": "blocked", "message": "Request must contain one profile alias."},
            status_code=400,
        )

    result = await asyncio.to_thread(sign_in, payload["alias"])
    return JSONResponse(result)


def _sign_in(alias: str) -> LoginResult:
    """Sign in with a configured persona alias, never accepting credentials.

    The helper resolves the alias from its private credential source. Password
    submissions are rate-limited to prevent rapid model retries.
    """
    global password_submission_at
    now = time.monotonic()
    if (
        password_submission_at is not None
        and now - password_submission_at < PASSWORD_RETRY_COOLDOWN_SECONDS
    ):
        return {
            "status": "blocked",
            "message": "A password was submitted recently. Stop this request and continue on a later cycle.",
        }

    credentials = _credentials_for(alias)
    if credentials is None:
        return {
            "status": "blocked",
            "message": "No credentials are configured for that profile alias.",
        }

    node = shutil.which("node")
    if not node:
        return {
            "status": "blocked",
            "message": "Node.js is unavailable to the login helper.",
        }

    result = _run_browser_login(node, *credentials)
    status = result.get("status")
    submission_started = bool(result.pop("credential_submission_started", False))
    if status == "authenticated":
        if not submission_started:
            return {
                "status": "login_unconfirmed",
                "message": PUBLIC_MESSAGES["login_unconfirmed"],
            }
        result["profile_alias"] = alias
    if status == "authentication_failed" or submission_started:
        password_submission_at = time.monotonic()
    if HARNESS_PROFILE:
        attempts.append(dict(result))
    return result


def _run_browser_login(node: str, username: str, password: str) -> LoginResult:
    """Run the isolated browser flow and accept only its public status result."""
    helper_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "UI_TEST_USERNAME": username,
        "UI_TEST_PASSWORD": password,
        "UI_TEST_TARGET_URL": TARGET_URL,
        "UI_TEST_CDP_URL": CDP_URL,
        "UI_BROWSER_PROXY": os.environ.get("UI_BROWSER_PROXY", ""),
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
            "PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"
        ),
    }
    if HARNESS_PROFILE:
        helper_env["UI_HARNESS_SHOW_COOKIE_PROMPT"] = (
            "1" if HARNESS_SHOW_COOKIE_PROMPT else ""
        )
    process = None
    try:
        process = subprocess.Popen(
            [node, str(LOGIN_SCRIPT)],
            env=helper_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        stdout, _ = process.communicate(timeout=BROWSER_LOGIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        return dict(SIGN_IN_INCOMPLETE)
    except OSError:
        return {
            "status": "blocked",
            "message": PUBLIC_MESSAGES["blocked"],
            "credential_submission_started": False,
        }

    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        return dict(SIGN_IN_INCOMPLETE)
    status = payload.get("status") if isinstance(payload, dict) else None
    if (
        process.returncode != 0
        or not isinstance(status, str)
        or status not in PUBLIC_MESSAGES
    ):
        return dict(SIGN_IN_INCOMPLETE)
    return _public_login_result(payload)


def sign_in(alias: str) -> LoginResult:
    """Serialize requests so concurrent calls cannot submit credentials twice."""
    with login_lock:
        return _sign_in(alias)


def _chromium_cdp_ready() -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"{CDP_URL}/json/version", timeout=1) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _find_chromium() -> str | None:
    browser_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if not browser_path.is_dir():
        return None
    return next(
        (
            str(candidate)
            for candidate in sorted(browser_path.rglob("chrome"))
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )


def _start_chromium(profile_dir: Path) -> subprocess.Popen | None:
    """Start Chromium with the instance proxy, without changing shared presets."""
    if _chromium_cdp_ready():
        return None

    chrome = _find_chromium()
    if not chrome:
        raise RuntimeError("The login helper could not find Playwright Chromium.")
    proxy = os.environ.get("UI_BROWSER_PROXY", "").strip()
    if not proxy:
        raise RuntimeError(
            "UI_BROWSER_PROXY must be set before the helper starts Chromium."
        )

    command = [
        chrome,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--disable-sync",
        "--disable-extensions",
        "--disable-popup-blocking",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--ignore-certificate-errors",
        "--host-resolver-rules=MAP consent.trustarc.com 127.0.0.1",
        "--proxy-bypass-list=localhost;127.0.0.1;::1",
        f"--user-data-dir={profile_dir / 'profile'}",
        f"--proxy-server={proxy}",
    ]

    browser_env = {
        key: os.environ[key]
        for key in ("HOME", "LANG", "LC_ALL", "PATH", "PLAYWRIGHT_BROWSERS_PATH")
        if os.environ.get(key)
    }
    browser_env["HOME"] = str(profile_dir)
    process = subprocess.Popen(
        command,
        env=browser_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "Chromium exited before its DevTools endpoint became ready."
            )
        if _chromium_cdp_ready():
            return process
        time.sleep(1)

    process.terminate()
    process.wait(timeout=5)
    raise RuntimeError("Chromium DevTools did not become ready within 60 seconds.")


def create_app() -> Starlette:
    routes = [
        Route("/health", health),
        Route("/sign-in", sign_in_endpoint, methods=["POST"]),
    ]
    if HARNESS_PROFILE:
        routes.append(Route("/__harness/state", harness_state))
    return Starlette(routes=routes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("stage login helper may bind only to loopback")
    import uvicorn

    # A fixed path prevents abandoned profiles from accumulating after restarts.
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    PROFILE_DIR.mkdir(parents=True)
    browser = None
    try:
        browser = _start_chromium(PROFILE_DIR)
        uvicorn.run(
            create_app(),
            host=args.host,
            port=args.port,
            access_log=False,
            log_level="warning",
        )
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
            browser.wait(timeout=10)
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
