"""Loopback-only fake console with an accessible two-step login flow."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HARNESS_DIR = Path(__file__).resolve().parents[1]
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

from fake_auth import (  # noqa: E402
    LoginResult,
    Profile,
    ProfileMetadata,
    load_profiles,
)


def _profiles() -> dict[str, Profile]:
    return load_profiles()


def authenticate(username: str, password: str) -> tuple[int, LoginResult]:
    """Authenticate fixture values and return display-safe identity data."""
    identity = next(
        (
            (alias, profile)
            for alias, profile in _profiles().items()
            if username == profile["username"] and password == profile["password"]
        ),
        None,
    )
    if identity is None:
        return 401, {"authenticated": False, "error": "Sign-in failed"}
    alias, profile = identity
    return 200, {
        "authenticated": True,
        "alias": alias,
        "auth_context": profile["auth_context"],
        "org_display": profile["org_display"],
    }


def authenticate_configured(alias: str) -> tuple[int, LoginResult]:
    """Sign in the selected local profile without exposing its credentials."""
    profiles = _profiles()
    configured_username = os.environ.get("UI_HARNESS_USERNAME")
    configured_password = os.environ.get("UI_HARNESS_PASSWORD")
    selected_alias = os.environ.get("UI_HARNESS_PROFILE", "viewer")
    if (configured_username or configured_password) and alias != selected_alias:
        return 403, {
            "authenticated": False,
            "error": "Requested fixture profile is not configured",
        }
    profile = profiles.get(alias)
    if profile is None:
        return 404, {"authenticated": False, "error": "Unknown fixture profile"}
    return authenticate(profile["username"], profile["password"])


class FakeHccServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address=("127.0.0.1", 0)):
        if address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Fake HCC must bind to loopback")
        super().__init__(address, FakeHccHandler)
        self.login_events: list[ProfileMetadata] = []


class FakeHccHandler(BaseHTTPRequestHandler):
    server_version = "LocalUiTestFixture/1.0"

    def log_message(self, _format, *_args):
        # Request bodies and form values must never enter logs.
        return

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/__harness/state":
            self._json(200, {"login_events": self.server.login_events})
            return
        if route not in {"/", "/login"}:
            self.send_error(404)
            return
        body = _LOGIN_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = urlparse(self.path).path
        if route not in {"/auth", "/auth/configured"}:
            self.send_error(404)
            return
        try:
            size = min(int(self.headers.get("Content-Length", "0")), 4096)
            payload = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"authenticated": False, "error": "invalid request"})
            return

        if route == "/auth/configured":
            status, result = authenticate_configured(payload.get("alias", ""))
        else:
            status, result = authenticate(
                payload.get("username", ""), payload.get("password", "")
            )
        if status == 200:
            self.server.login_events.append(
                {
                    "alias": result["alias"],
                    "auth_context": result["auth_context"],
                    "org_display": result["org_display"],
                }
            )
        self._json(status, result)

    def _json(self, status: int, value: LoginResult | dict[str, object]) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Local Console Fixture</title></head>
<body>
  <main>
    <h1>Sign in to the UI test fixture</h1>
    <section aria-labelledby="configured-login-heading">
      <h2 id="configured-login-heading">Configured test account</h2>
      <label for="test-profile">Test profile</label>
      <select id="test-profile">
        <option value="viewer">viewer</option>
        <option value="org-admin">org-admin</option>
      </select>
      <button type="button" id="configured-sign-in">Sign in with configured test account</button>
    </section>
    <h2>Manual sign-in</h2>
    <form id="login-form">
      <section id="username-step">
        <label for="username">Username</label>
        <input id="username" name="username" autocomplete="username" required>
        <button type="button" id="continue">Continue</button>
      </section>
      <section id="password-step" hidden>
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Sign in</button>
      </section>
    </form>
    <p id="status" role="status" aria-live="polite"></p>
    <section id="console" hidden>
      <h2>Console home</h2>
      <p id="identity"></p>
      <nav aria-label="Console navigation">
        <button type="button" id="subscriptions-link">Subscriptions</button>
        <a href="#inventory" id="inventory-link">Inventory</a>
      </nav>
      <section id="subscriptions" hidden>
        <h3>Subscriptions</h3>
        <p>Two subscriptions are available.</p>
      </section>
      <section id="inventory" hidden>
        <h3>Inventory</h3>
        <table>
          <caption>Registered systems</caption>
          <thead><tr><th scope="col">Name</th><th scope="col">Status</th></tr></thead>
          <tbody><tr><td>fixture-system-01</td><td>Connected</td></tr></tbody>
        </table>
      </section>
      <section id="organization-settings" hidden>
        <h3>Organization settings</h3>
        <p>Administrative tools are available to this fixture profile.</p>
      </section>
    </section>
  </main>
  <script>
    const usernameStep = document.querySelector('#username-step');
    const passwordStep = document.querySelector('#password-step');
    const username = document.querySelector('#username');
    const password = document.querySelector('#password');
    const status = document.querySelector('#status');
    const showSignedInConsole = (result) => {
      status.textContent = result.authenticated ? 'Signed in' : result.error;
      if (result.authenticated) {
        document.querySelector('#console').hidden = false;
        document.querySelector('#identity').textContent = result.auth_context + ' — ' + result.org_display;
        if (result.auth_context === 'Organization administrator') {
          document.querySelector('#organization-settings').hidden = false;
        }
        password.value = '';
      }
    };
    document.querySelector('#configured-sign-in').addEventListener('click', async () => {
      const response = await fetch('/auth/configured', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({alias: document.querySelector('#test-profile').value}),
      });
      showSignedInConsole(await response.json());
    });
    document.querySelector('#subscriptions-link').addEventListener('click', () => {
      document.querySelector('#subscriptions').hidden = false;
    });
    document.querySelector('#inventory-link').addEventListener('click', () => {
      document.querySelector('#inventory').hidden = false;
    });
    document.querySelector('#continue').addEventListener('click', () => {
      if (!username.value) { username.reportValidity(); return; }
      usernameStep.hidden = true;
      passwordStep.hidden = false;
      password.focus();
    });
    document.querySelector('#login-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const response = await fetch('/auth', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: username.value, password: password.value})});
      showSignedInConsole(await response.json());
    });
  </script>
</body>
</html>"""


if __name__ == "__main__":
    server = FakeHccServer(("127.0.0.1", 8765))
    print("Local HCC fixture available at http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
