from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

HELPER_PATH = (
    Path(__file__).resolve().parents[2] / "agent" / "login-helper" / "server.py"
)
ENVIRONMENT_KEYS = (
    "UI_TEST_CREDENTIALS_DIR",
    "UI_TEST_USERNAME",
    "UI_TEST_PASSWORD",
    "UI_HARNESS_PROFILE",
    "UI_HARNESS_SHOW_COOKIE_PROMPT",
)


class FakeRoute:
    def __init__(self, path, endpoint, methods=None):
        self.path = path
        self.endpoint = endpoint
        self.methods = methods


class FakeStarlette:
    def __init__(self, routes):
        self.routes = routes


class FakeJSONResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def body(self):
        return self._body


def load_helper_module():
    """Import the helper with small Starlette stubs for offline testing."""
    starlette = ModuleType("starlette")
    starlette.__path__ = []
    applications = ModuleType("starlette.applications")
    applications.Starlette = FakeStarlette
    requests = ModuleType("starlette.requests")
    requests.Request = type("Request", (), {})
    responses = ModuleType("starlette.responses")
    responses.JSONResponse = FakeJSONResponse
    routing = ModuleType("starlette.routing")
    routing.Route = FakeRoute

    stubs = {
        "starlette": starlette,
        "starlette.applications": applications,
        "starlette.requests": requests,
        "starlette.responses": responses,
        "starlette.routing": routing,
    }
    saved_environment = {name: os.environ.get(name) for name in ENVIRONMENT_KEYS}
    for name in ENVIRONMENT_KEYS:
        os.environ.pop(name, None)

    module_name = "ui_test_login_helper_under_test"
    try:
        with patch.dict(sys.modules, stubs):
            spec = importlib.util.spec_from_file_location(module_name, HELPER_PATH)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
            return module
    finally:
        for name, value in saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def completed_login(status="authenticated", submitted=True):
    return SimpleNamespace(
        returncode=0,
        communicate=Mock(
            return_value=(
                json.dumps(
                    {
                        "status": status,
                        "credential_submission_started": submitted,
                        "message": "secret-pass test-user",
                        "password": "secret-pass",
                    }
                ),
                None,
            )
        ),
    )


class LoginHelperTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.helper.CREDENTIALS_DIR = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_credentials(
        self, alias="org-admin", username="test-user", password="secret-pass"
    ):
        (self.helper.CREDENTIALS_DIR / f"{alias}.username").write_text(
            f"{username}\n", encoding="utf-8"
        )
        (self.helper.CREDENTIALS_DIR / f"{alias}.password").write_text(
            f"{password}\n", encoding="utf-8"
        )

    def test_resolves_a_complete_file_profile_and_strips_newlines(self):
        self.write_credentials()
        self.assertEqual(
            self.helper._credentials_for("org-admin"),
            ("test-user", "secret-pass"),
        )

    def test_rejects_invalid_unknown_and_incomplete_profiles(self):
        self.write_credentials(alias="viewer", password="")
        self.assertIsNone(self.helper._credentials_for("../org-admin"))
        self.assertIsNone(self.helper._credentials_for("unknown"))
        self.assertIsNone(self.helper._credentials_for("viewer"))

    def test_harness_profile_uses_only_its_explicit_environment_credentials(self):
        self.helper.HARNESS_PROFILE = "viewer"
        self.helper.HARNESS_USERNAME = "fixture-user"
        self.helper.HARNESS_PASSWORD = "fixture-pass"

        self.assertEqual(
            self.helper._credentials_for("viewer"),
            ("fixture-user", "fixture-pass"),
        )
        self.assertIsNone(self.helper._credentials_for("org-admin"))

    def test_sign_in_passes_credentials_only_to_child_and_sanitizes_its_output(self):
        self.write_credentials()
        process = completed_login()
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(
                self.helper.subprocess, "Popen", return_value=process
            ) as popen,
        ):
            result = self.helper.sign_in("org-admin")

        passed_environment = popen.call_args.kwargs["env"]
        self.assertEqual(passed_environment["UI_TEST_USERNAME"], "test-user")
        self.assertEqual(passed_environment["UI_TEST_PASSWORD"], "secret-pass")
        self.assertNotIn("JIRA_TOKEN", passed_environment)
        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["profile_alias"], "org-admin")
        self.assertNotIn("secret-pass", json.dumps(result))
        self.assertNotIn("test-user", json.dumps(result))
        self.assertEqual(
            process.communicate.call_args.kwargs["timeout"],
            self.helper.BROWSER_LOGIN_TIMEOUT_SECONDS,
        )

    def test_credentials_are_entered_before_session_transfer_to_shared_cdp(self):
        script = (HELPER_PATH.parent / "browser-login.mjs").read_text(encoding="utf-8")
        login_flow = script.split("async function runLogin", 1)[1].split(
            "async function closeBrowsers", 1
        )[0]
        private_launch = login_flow.split("browsers.login = loginBrowser", 1)[0]

        self.assertLess(
            login_flow.index("chromium.launch"),
            login_flow.index("await redHatSsoLogin"),
        )
        self.assertLess(
            login_flow.index("await redHatSsoLogin"),
            login_flow.index("await transferSession(await context.storageState()"),
        )
        self.assertNotIn("connectOverCDP", login_flow)
        self.assertNotIn("--remote-debugging-port", private_launch)
        self.assertIn("delete env.UI_TEST_USERNAME", script)
        self.assertIn("delete env.UI_TEST_PASSWORD", script)

    def test_successful_submission_starts_the_password_cooldown(self):
        self.write_credentials()
        process = completed_login()
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(
                self.helper.subprocess, "Popen", return_value=process
            ) as popen,
            patch.object(self.helper.time, "monotonic", side_effect=[100, 101, 102]),
        ):
            first = self.helper.sign_in("org-admin")
            second = self.helper.sign_in("org-admin")

        self.assertEqual(first["status"], "authenticated")
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(popen.call_count, 1)

    def test_failed_submission_blocks_another_password_attempt(self):
        self.write_credentials()
        process = completed_login("authentication_failed")
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(
                self.helper.subprocess, "Popen", return_value=process
            ) as popen,
        ):
            first = self.helper.sign_in("org-admin")
            second = self.helper.sign_in("org-admin")

        self.assertEqual(first["status"], "authentication_failed")
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(popen.call_count, 1)

    def test_later_cycle_can_switch_personas_after_the_cooldown(self):
        self.write_credentials()
        self.write_credentials("non-org-admin", "viewer-user", "viewer-pass")
        processes = [completed_login(), completed_login()]
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(
                self.helper.subprocess, "Popen", side_effect=processes
            ) as popen,
            patch.object(
                self.helper.time, "monotonic", side_effect=[100, 101, 401, 402]
            ),
        ):
            first = self.helper.sign_in("org-admin")
            second = self.helper.sign_in("non-org-admin")

        self.assertEqual(first["profile_alias"], "org-admin")
        self.assertEqual(second["profile_alias"], "non-org-admin")
        self.assertEqual(
            [call.kwargs["env"]["UI_TEST_USERNAME"] for call in popen.call_args_list],
            ["test-user", "viewer-user"],
        )

    def test_alias_is_not_reported_without_a_credential_submission(self):
        self.write_credentials()
        process = completed_login(submitted=False)
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "Popen", return_value=process),
        ):
            result = self.helper.sign_in("org-admin")

        self.assertEqual(result["status"], "login_unconfirmed")
        self.assertNotIn("profile_alias", result)

    def test_timeout_sends_sigterm_before_returning_a_blocker(self):
        self.write_credentials()
        process = SimpleNamespace(
            pid=123,
            returncode=-signal.SIGTERM,
            communicate=Mock(
                side_effect=[
                    subprocess.TimeoutExpired("node", 240),
                    ("", None),
                ]
            ),
            send_signal=Mock(),
        )
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "Popen", return_value=process),
        ):
            result = self.helper.sign_in("org-admin")

        self.assertEqual(result["status"], "blocked")
        process.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_unknown_alias_is_not_echoed_or_sent_to_node(self):
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "Popen") as popen,
        ):
            result = self.helper.sign_in("private-alias-name")

        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("profile_alias", result)
        self.assertNotIn("private-alias-name", json.dumps(result))
        popen.assert_not_called()

    def test_http_endpoint_accepts_only_a_profile_alias(self):
        result = {"status": "authenticated", "profile_alias": "org-admin"}
        with patch.object(self.helper, "sign_in", return_value=result) as sign_in:
            response = asyncio.run(
                self.helper.sign_in_endpoint(FakeRequest(b'{"alias":"org-admin"}'))
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, result)
        sign_in.assert_called_once_with("org-admin")

    def test_health_requires_a_live_chromium_endpoint(self):
        with patch.object(self.helper, "_chromium_cdp_ready", return_value=False):
            response = asyncio.run(self.helper.health(FakeRequest(b"")))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content, {"status": "browser_unavailable"})

    def test_production_app_does_not_expose_harness_state(self):
        paths = [route.path for route in self.helper.create_app().routes]
        self.assertNotIn("/__harness/state", paths)

    def test_harness_state_is_bounded_and_enabled_only_in_local_mode(self):
        self.helper.HARNESS_PROFILE = "viewer"
        self.helper.attempts.clear()
        for index in range(self.helper.attempts.maxlen + 2):
            self.helper.attempts.append({"status": "blocked", "index": index})

        paths = [route.path for route in self.helper.create_app().routes]
        response = asyncio.run(self.helper.harness_state(FakeRequest(b"")))

        self.assertIn("/__harness/state", paths)
        self.assertEqual(len(response.content["attempts"]), 100)
        self.assertEqual(response.content["attempts"][0]["index"], 2)

    def test_http_endpoint_rejects_credentials_and_extra_fields(self):
        request = FakeRequest(
            b'{"alias":"org-admin","password":"must-not-be-accepted"}'
        )
        with patch.object(self.helper, "sign_in") as sign_in:
            response = asyncio.run(self.helper.sign_in_endpoint(request))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content["status"], "blocked")
        self.assertNotIn("must-not-be-accepted", json.dumps(response.content))
        sign_in.assert_not_called()

    def test_http_endpoint_rejects_invalid_and_oversized_bodies(self):
        for body in (b"not-json", b" " * 257):
            with self.subTest(body_size=len(body)):
                response = asyncio.run(self.helper.sign_in_endpoint(FakeRequest(body)))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.content["status"], "blocked")

    def test_chromium_is_not_started_when_shared_cdp_is_already_ready(self):
        with (
            patch.object(self.helper, "_chromium_cdp_ready", return_value=True),
            patch.object(self.helper.subprocess, "Popen") as popen,
        ):
            process = self.helper._start_chromium(Path(self.tempdir.name))

        self.assertIsNone(process)
        popen.assert_not_called()

    def test_chromium_uses_only_the_helper_browser_proxy(self):
        process = SimpleNamespace(poll=lambda: None)
        with (
            patch.object(self.helper, "_chromium_cdp_ready", side_effect=[False, True]),
            patch.object(self.helper, "_find_chromium", return_value="/tmp/chrome"),
            patch.object(
                self.helper.subprocess, "Popen", return_value=process
            ) as popen,
            patch.dict(os.environ, {"UI_BROWSER_PROXY": "http://proxy:3128"}),
        ):
            started = self.helper._start_chromium(Path(self.tempdir.name))

        self.assertIs(started, process)
        command = popen.call_args.args[0]
        self.assertIn("--proxy-server=http://proxy:3128", command)
        self.assertEqual(popen.call_args.kwargs["env"].get("UI_BROWSER_PROXY"), None)
        self.assertNotIn("UI_TEST_USERNAME", popen.call_args.kwargs["env"])
        self.assertIn(f"--user-data-dir={self.tempdir.name}/profile", command)

    def test_chromium_does_not_start_without_a_browser_proxy(self):
        with (
            patch.object(self.helper, "_chromium_cdp_ready", return_value=False),
            patch.object(self.helper, "_find_chromium", return_value="/tmp/chrome"),
            patch.object(self.helper.subprocess, "Popen") as popen,
            patch.dict(os.environ, {"UI_BROWSER_PROXY": ""}),
            self.assertRaisesRegex(RuntimeError, "UI_BROWSER_PROXY"),
        ):
            self.helper._start_chromium(Path(self.tempdir.name))

        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
