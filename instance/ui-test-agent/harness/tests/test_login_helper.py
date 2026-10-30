from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

HELPER_PATH = (
    Path(__file__).resolve().parents[2] / "agent" / "login-helper" / "server.py"
)
ENVIRONMENT_KEYS = (
    "UI_TEST_USERNAME",
    "UI_TEST_PASSWORD",
    "UI_HARNESS_PROFILE",
    "UI_TEST_CREDENTIALS_DIR",
)


class FakeFastMCP:
    def __init__(self, _name: str):
        self.tools = []

    def custom_route(self, *_args, **_kwargs):
        return lambda function: function

    def tool(self):
        def register(function):
            self.tools.append(function)
            return function

        return register


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
    """Import the helper with small MCP/Starlette stubs for offline testing."""
    mcp = ModuleType("mcp")
    mcp.__path__ = []
    mcp_server = ModuleType("mcp.server")
    mcp_server.__path__ = []
    fastmcp = ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = FakeFastMCP
    starlette = ModuleType("starlette")
    starlette.__path__ = []
    requests = ModuleType("starlette.requests")
    requests.Request = type("Request", (), {})
    responses = ModuleType("starlette.responses")
    responses.JSONResponse = FakeJSONResponse

    stubs = {
        "mcp": mcp,
        "mcp.server": mcp_server,
        "mcp.server.fastmcp": fastmcp,
        "starlette": starlette,
        "starlette.requests": requests,
        "starlette.responses": responses,
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
        self.assertTrue(self.helper._is_configured_alias("org-admin"))

    def test_rejects_invalid_unknown_and_incomplete_profiles(self):
        self.write_credentials(alias="viewer", password="")

        self.assertIsNone(self.helper._credentials_for("../org-admin"))
        self.assertIsNone(self.helper._credentials_for("unknown"))
        self.assertIsNone(self.helper._credentials_for("viewer"))
        self.assertFalse(self.helper._is_configured_alias("../org-admin"))

    def test_environment_profile_requires_matching_alias_and_both_values(self):
        self.helper.USERNAME = "env-user"
        self.helper.PASSWORD = "env-pass"
        self.helper.HARNESS_ALIAS = "viewer"

        self.assertEqual(
            self.helper._credentials_for("viewer"), ("env-user", "env-pass")
        )
        self.assertIsNone(self.helper._credentials_for("org-admin"))

        self.helper.PASSWORD = ""
        self.assertIsNone(self.helper._credentials_for("viewer"))

    def test_sign_in_passes_credentials_only_to_child_and_sanitizes_its_output(self):
        self.write_credentials()
        child_output = json.dumps(
            {
                "status": "authenticated",
                "credential_submission_started": True,
                "message": "secret-pass test-user",
                "password": "secret-pass",
            }
        )
        completed = SimpleNamespace(returncode=0, stdout=child_output, stderr="")

        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "run", return_value=completed) as run,
        ):
            result = self.helper.sign_in("org-admin")

        passed_environment = run.call_args.kwargs["env"]
        self.assertEqual(passed_environment["UI_TEST_USERNAME"], "test-user")
        self.assertEqual(passed_environment["UI_TEST_PASSWORD"], "secret-pass")
        self.assertNotIn("JIRA_TOKEN", passed_environment)
        self.assertEqual(result["status"], "authenticated")
        self.assertEqual(result["profile_alias"], "org-admin")
        self.assertNotIn("secret-pass", json.dumps(result))
        self.assertNotIn("test-user", json.dumps(self.helper.attempts))
        self.assertNotIn("secret-pass", json.dumps(self.helper.attempts))

    def test_failed_submission_blocks_another_password_attempt(self):
        self.write_credentials()
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "authentication_failed"}),
            stderr="",
        )

        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "run", return_value=completed) as run,
        ):
            first = self.helper.sign_in("org-admin")
            second = self.helper.sign_in("org-admin")

        self.assertEqual(first["status"], "authentication_failed")
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(run.call_count, 1)

    def test_failed_submission_cooldown_expires_for_a_later_cycle(self):
        self.write_credentials()
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "authentication_failed"}),
            stderr="",
        )

        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "run", return_value=completed) as run,
            patch.object(
                self.helper.time, "monotonic", side_effect=[100, 101, 401, 402]
            ),
        ):
            first = self.helper.sign_in("org-admin")
            second = self.helper.sign_in("org-admin")

        self.assertEqual(first["status"], "authentication_failed")
        self.assertEqual(second["status"], "authentication_failed")
        self.assertEqual(run.call_count, 2)

    def test_unknown_alias_is_not_echoed_or_sent_to_node(self):
        with (
            patch.object(self.helper.shutil, "which", return_value="/usr/bin/node"),
            patch.object(self.helper.subprocess, "run") as run,
        ):
            result = self.helper.sign_in("private-alias-name")

        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("profile_alias", result)
        self.assertNotIn("private-alias-name", json.dumps(result))
        self.assertNotIn("private-alias-name", json.dumps(self.helper.attempts))
        run.assert_not_called()

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
            process = self.helper._start_chromium()

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
            started = self.helper._start_chromium()

        self.assertIs(started, process)
        command = popen.call_args.args[0]
        self.assertIn("--proxy-server=http://proxy:3128", command)
        self.assertEqual(popen.call_args.kwargs["env"].get("UI_BROWSER_PROXY"), None)
        self.assertNotIn("UI_TEST_USERNAME", popen.call_args.kwargs["env"])

    def test_chromium_does_not_start_without_a_browser_proxy(self):
        with (
            patch.object(self.helper, "_chromium_cdp_ready", return_value=False),
            patch.object(self.helper, "_find_chromium", return_value="/tmp/chrome"),
            patch.object(self.helper.subprocess, "Popen") as popen,
            patch.dict(os.environ, {"UI_BROWSER_PROXY": ""}),
            self.assertRaisesRegex(RuntimeError, "UI_BROWSER_PROXY"),
        ):
            self.helper._start_chromium()

        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
