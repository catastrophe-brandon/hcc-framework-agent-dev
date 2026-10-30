from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

HARNESS_DIR = Path(__file__).resolve().parents[1]
INSTANCE_DIR = HARNESS_DIR.parent
sys.path.insert(0, str(HARNESS_DIR))

import fake_mcp_server  # noqa: E402
import run_agent_cycle  # noqa: E402
from fake_auth import (  # noqa: E402
    UnknownProfileError,
    login_profile,
    profile_metadata,
)
from fake_services import FakeJira, FakeMemory  # noqa: E402

PREFLIGHT = (
    INSTANCE_DIR
    / "agent"
    / "workflows"
    / "ui-test"
    / "preflight"
    / "01-find-ui-test-work.py"
)


class QueryParamsFixture:
    def __init__(self, values: dict[str, list[str]]):
        self.values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        return next(iter(self.values.get(key, [])), default)

    def getlist(self, key: str) -> list[str]:
        return self.values.get(key, [])


def issue(key="UITEST-LOCAL-1", updated="2026-01-01T00:00:00Z", comments=None):
    return {
        "key": key,
        "fields": {
            "summary": "Exercise the local console fixture",
            "status": {"name": "To Do"},
            "labels": ["ui-test"],
            "updated": updated,
            "created": "2025-12-01T00:00:00Z",
        },
        "comments": comments or [],
    }


def run_preflight(
    fake_jira: FakeJira,
    fake_memory: FakeMemory,
    capacity=(0, 5),
    instance_id="ui-test-harness",
    label="ui-test",
):
    results = []
    common = ModuleType("common")
    common.INSTANCE_ID = instance_id
    common.get_capacity = lambda: capacity
    common.output_result = lambda status, text="": results.append((status, text))

    jira_module = ModuleType("jira_mcp")
    jira_module.jira_call = fake_jira.call
    jira_module.jira_cleanup = lambda: None

    memory_module = ModuleType("memory_mcp")
    memory_module.memory_call = fake_memory.call
    memory_module.memory_cleanup = lambda: None

    injected = {"common": common, "jira_mcp": jira_module, "memory_mcp": memory_module}
    old_modules = {name: sys.modules.get(name) for name in injected}
    old_label = os.environ.get("BOT_LABEL")
    os.environ["BOT_LABEL"] = label
    try:
        sys.modules.update(injected)
        spec = importlib.util.spec_from_file_location(
            "ui_test_preflight_under_test", PREFLIGHT
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        module.main()
    finally:
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
        if old_label is None:
            os.environ.pop("BOT_LABEL", None)
        else:
            os.environ["BOT_LABEL"] = old_label
    return results


def task_record(status, last_addressed="2026-01-15T12:00:00Z"):
    return {
        "external_key": "UITEST-LOCAL-1",
        "source_type": "jira",
        "instance_id": "ui-test-harness",
        "status": status,
        "last_addressed": last_addressed,
    }


def jira_comment(body, created, author="Developer"):
    return {
        "body": body,
        "created": created,
        "author": {"displayName": author},
    }


class PreflightHarnessTests(unittest.TestCase):
    def test_missing_instance_id_fails_before_external_lookups(self):
        jira = FakeJira([issue()])
        memory = FakeMemory()

        results = run_preflight(jira, memory, instance_id="")

        self.assertEqual(results[0][0], "error")
        self.assertIn("BOT_INSTANCE_ID is required", results[0][1])
        self.assertEqual(jira.calls, [])
        self.assertEqual(memory.calls, [])

    def test_unsafe_label_fails_before_external_lookups(self):
        jira = FakeJira([issue()])
        memory = FakeMemory()

        results = run_preflight(jira, memory, label='ui-test" OR project is not EMPTY')

        self.assertEqual(results[0][0], "error")
        self.assertIn("BOT_LABEL contains unsupported characters", results[0][1])
        self.assertEqual(jira.calls, [])
        self.assertEqual(memory.calls, [])

    def test_no_labeled_ticket_skips(self):
        jira = FakeJira([issue()])
        jira.issues["UITEST-LOCAL-1"]["fields"]["labels"] = ["another-label"]
        results = run_preflight(jira, FakeMemory())
        self.assertEqual(results[0][0], "skip")
        self.assertEqual([name for name, _ in jira.calls], ["jira_search"])

    def test_new_labeled_ticket_is_selected(self):
        jira = FakeJira([issue()])
        memory = FakeMemory()
        results = run_preflight(jira, memory)
        self.assertEqual(results[0][0], "start")
        self.assertIn("UITEST-LOCAL-1", results[0][1])
        self.assertIn("new labeled UI-testing ticket", results[0][1])
        self.assertEqual(jira.calls[0][0], "jira_search")
        self.assertEqual(memory.calls[0][0], "task_list")

    def test_new_ticket_waits_when_task_capacity_is_full(self):
        jira = FakeJira([issue()])

        results = run_preflight(jira, FakeMemory(), capacity=(5, 5))

        self.assertEqual(results[0][0], "skip")
        self.assertIn("capacity is full", results[0][1])

    def test_active_task_resumes_without_rechecking_comments(self):
        jira = FakeJira([issue()])
        memory = FakeMemory(tasks=[task_record("in_progress")])

        results = run_preflight(jira, memory)

        self.assertEqual(results[0][0], "start")
        self.assertIn("resume interrupted test work", results[0][1])
        self.assertEqual([name for name, _ in jira.calls], ["jira_search"])

    def test_paused_task_resumes_after_a_new_adf_comment(self):
        comment = jira_comment(
            {
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Please retry the flow."}],
                    }
                ]
            },
            "2026-02-01T12:00:00Z",
        )
        jira = FakeJira([issue(updated="2026-02-01T12:00:01Z", comments=[comment])])
        memory = FakeMemory(tasks=[task_record("paused")])

        results = run_preflight(jira, memory)

        self.assertEqual(results[0][0], "start")
        self.assertIn("human reply received", results[0][1])
        self.assertIn("Please retry the flow.", results[0][1])

    def test_completed_task_routes_new_human_followup_not_report_mention(self):
        comments = [
            jira_comment(
                "**UI Testing Agent Report**\nPrevious result.",
                "2026-02-01T12:00:00Z",
                author="UI Testing Agent",
            ),
            jira_comment(
                "The UI Testing Agent Report missed the settings menu; please check it.",
                "2026-02-02T12:00:00Z",
            ),
        ]
        jira = FakeJira([issue(updated="2026-02-02T12:00:01Z", comments=comments)])
        memory = FakeMemory(tasks=[task_record("done")])

        results = run_preflight(jira, memory)

        self.assertEqual(results[0][0], "start")
        self.assertIn("assess whether it requests a retest", results[0][1])
        self.assertIn("missed the settings menu", results[0][1])

    def test_completed_task_ignores_old_and_agent_generated_comments(self):
        comments = [
            jira_comment("Old request", "2026-01-10T12:00:00Z"),
            jira_comment(
                " **UI Testing Agent Clarification**\nQuestion.",
                "2026-02-01T12:00:00Z",
                author="UI Testing Agent",
            ),
            jira_comment("  ", "2026-02-02T12:00:00Z"),
        ]
        jira = FakeJira([issue(updated="2026-02-02T12:00:01Z", comments=comments)])
        memory = FakeMemory(tasks=[task_record("done")])

        results = run_preflight(jira, memory)

        self.assertEqual(results[0][0], "skip")
        self.assertEqual(
            [name for name, _ in jira.calls], ["jira_search", "jira_get_issue"]
        )

    def test_retest_comment_dispatches_existing_completed_ticket(self):
        comments = [jira_comment("/retest", "2026-02-01T12:00:00Z")]
        jira = FakeJira([issue(updated="2026-02-01T12:00:01Z", comments=comments)])
        memory = FakeMemory(tasks=[task_record("done")])

        results = run_preflight(jira, memory)
        self.assertEqual(results[0][0], "start")
        self.assertIn("explicit retest request", results[0][1])
        self.assertIn("/retest", results[0][1])
        self.assertEqual(
            [name for name, _ in jira.calls], ["jira_search", "jira_get_issue"]
        )

    def test_completed_followup_waits_when_task_capacity_is_full(self):
        comments = [jira_comment("Please retest", "2026-02-01T12:00:00Z")]
        jira = FakeJira([issue(updated="2026-02-01T12:00:01Z", comments=comments)])
        memory = FakeMemory(tasks=[task_record("done")])

        results = run_preflight(jira, memory, capacity=(5, 5))

        self.assertEqual(results[0][0], "skip")
        self.assertIn("UITEST-LOCAL-1", results[0][1])
        self.assertIn("capacity is full", results[0][1])

    def test_memory_failure_stops_before_jira_search(self):
        class UnavailableMemory(FakeMemory):
            def call(self, name, arguments):
                if name == "task_list":
                    return None
                return super().call(name, arguments)

        jira = FakeJira([issue()])

        results = run_preflight(jira, UnavailableMemory())

        self.assertEqual(results[0][0], "error")
        self.assertIn("Memory task lookup failed", results[0][1])
        self.assertEqual(jira.calls, [])

    def test_jira_search_failure_returns_error(self):
        class UnavailableJira(FakeJira):
            def call(self, name, arguments):
                if name == "jira_search":
                    return None
                return super().call(name, arguments)

        results = run_preflight(UnavailableJira([issue()]), FakeMemory())

        self.assertEqual(results[0][0], "error")
        self.assertIn("Jira search failed", results[0][1])

    def test_comment_read_failure_returns_error_instead_of_repeating_work(self):
        class UnavailableComments(FakeJira):
            def call(self, name, arguments):
                if name == "jira_get_issue":
                    self.calls.append((name, arguments))
                    return None
                return super().call(name, arguments)

        comment = jira_comment("Please retest", "2026-02-01T12:00:00Z")
        jira = UnavailableComments(
            [issue(updated="2026-02-01T12:00:01Z", comments=[comment])]
        )
        memory = FakeMemory(tasks=[task_record("done")])

        results = run_preflight(jira, memory)

        self.assertEqual(results[0][0], "error")
        self.assertIn("refusing to guess retest state", results[0][1])


class RuntimeHarnessTests(unittest.TestCase):
    def test_runner_copy_is_limited_to_one_cycle(self):
        source = (
            "def _read_sleep_signal():\n"
            "    time.sleep(sleep_seconds)\n"
            "\n"
            "def main():\n"
            "    try:\n"
            "        while True:\n"
            "            _try_slack_digest()\n"
            "            run_cycle()\n"
            "    finally:\n"
            "        release_lock()\n"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = Path(tmp_dir)
            bot_dir = runtime / "bot"
            bot_dir.mkdir()
            runner_module = bot_dir / "run.py"
            runner_module.write_text(source, encoding="utf-8")

            run_agent_cycle._limit_runtime_to_one_cycle(runtime)

            bounded_source = runner_module.read_text(encoding="utf-8")
            self.assertIn("for _cycle in range(1):", bounded_source)
            self.assertNotIn("while True:", bounded_source)
            self.assertNotIn("time.sleep(sleep_seconds)", bounded_source)
            self.assertIn("return 0", bounded_source)
            compile(bounded_source, str(runner_module), "exec")


class CycleGateTests(unittest.TestCase):
    def cycle_passes(self, **overrides):
        checks = {
            "target": "stage",
            "runner_exit": 0,
            "report_is_valid": True,
            "fixture_authenticated": False,
            "stage_login_attempts": [{"status": "authenticated"}],
            "learned": True,
            "credential_leak": False,
        }
        checks.update(overrides)
        return run_agent_cycle._cycle_passed(**checks)

    def test_stage_requires_confirmed_login_status(self):
        for status in ("authenticated", "already_authenticated"):
            with self.subTest(status=status):
                self.assertTrue(
                    self.cycle_passes(stage_login_attempts=[{"status": status}])
                )

        for status in (
            "credentials_submitted",
            "authentication_unconfirmed",
            "login_unconfirmed",
            "blocked",
            "unknown",
        ):
            with self.subTest(status=status):
                self.assertFalse(
                    self.cycle_passes(stage_login_attempts=[{"status": status}])
                )

        self.assertFalse(self.cycle_passes(stage_login_attempts=[]))

    def test_local_target_uses_fixture_authentication(self):
        self.assertTrue(
            self.cycle_passes(
                target="local",
                fixture_authenticated=True,
                stage_login_attempts=[],
            )
        )
        self.assertFalse(
            self.cycle_passes(
                target="local",
                fixture_authenticated=False,
                stage_login_attempts=[{"status": "authenticated"}],
            )
        )

    def test_other_cycle_checks_still_gate_success(self):
        failures = {
            "runner_exit": 1,
            "report_is_valid": False,
            "learned": False,
            "credential_leak": True,
        }
        for failed_check, value in failures.items():
            with self.subTest(failed_check=failed_check):
                self.assertFalse(self.cycle_passes(**{failed_check: value}))


class FakeServiceTests(unittest.TestCase):
    def test_fake_jira_records_comments_and_rejects_transitions(self):
        jira = FakeJira([issue()])
        jira.call(
            "jira_add_comment",
            {"issue_key": "UITEST-LOCAL-1", "comment": "fixture report"},
        )
        self.assertEqual(len(jira.issues["UITEST-LOCAL-1"]["comments"]), 1)
        with self.assertRaises(AssertionError):
            jira.call("jira_transition_issue", {"issue_key": "UITEST-LOCAL-1"})

    def test_fake_memory_keeps_distinct_runs(self):
        memory = FakeMemory()
        first = memory.call(
            "memory_store", {"external_key": "UITEST-LOCAL-1", "title": "run one"}
        )
        second = memory.call(
            "memory_store", {"external_key": "UITEST-LOCAL-1", "title": "run two"}
        )
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(memory.call("memory_list", {})), 2)

    def test_fake_memory_update_preserves_prior_metadata(self):
        memory = FakeMemory(
            tasks=[
                {
                    "external_key": "UITEST-LOCAL-1",
                    "status": "done",
                    "metadata": {"run_count": 1, "readiness": "Ready with risks"},
                }
            ]
        )
        updated = memory.call(
            "task_update",
            {
                "external_key": "UITEST-LOCAL-1",
                "status": "in_progress",
                "metadata": {"run_count": 2},
            },
        )
        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(
            updated["metadata"], {"run_count": 2, "readiness": "Ready with risks"}
        )

    def test_fake_mcp_refuses_non_loopback_bind(self):
        original_argv = sys.argv
        try:
            sys.argv = ["fake_mcp_server.py", "jira", "--host", "0.0.0.0"]
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    fake_mcp_server.main()
        finally:
            sys.argv = original_argv

    def test_fake_memory_rest_capacity_endpoint_filters_completed_work(self):
        memory = FakeMemory(
            tasks=[
                {
                    "external_key": "UITEST-A",
                    "status": "in_progress",
                    "instance_id": "ui-test-harness",
                },
                {
                    "external_key": "UITEST-B",
                    "status": "done",
                    "instance_id": "ui-test-harness",
                },
            ]
        )
        payload = fake_mcp_server.memory_tasks_payload(
            memory,
            QueryParamsFixture(
                {
                    "instance_id": ["ui-test-harness"],
                    "exclude_status": ["done", "paused", "archived"],
                }
            ),
        )
        self.assertEqual(
            [task["external_key"] for task in payload["items"]], ["UITEST-A"]
        )


class FakeAuthTests(unittest.TestCase):
    def test_alias_login_returns_only_safe_metadata(self):
        received = {}

        def capture(username, password):
            received["username"] = username
            received["password"] = password
            return {"authenticated": True}

        result = login_profile("viewer", capture)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["alias"], "viewer")
        self.assertEqual(result["org_display"], "UI Test Fixture Org")
        serialized = json.dumps(result)
        self.assertNotIn(received["username"], serialized)
        self.assertNotIn(received["password"], serialized)
        self.assertEqual(
            set(profile_metadata("viewer")), {"alias", "auth_context", "org_display"}
        )

        def failing_submit(_username, password):
            raise ValueError(f"fixture rejected {password}")

        with self.assertRaisesRegex(
            RuntimeError, "Local profile login failed"
        ) as raised:
            login_profile("viewer", failing_submit)
        self.assertNotIn(received["password"], str(raised.exception))

    def test_selected_fixture_alias_can_use_environment_credentials(self):
        received = {}

        def capture(username, password):
            received.update(username=username, password=password)
            return {"authenticated": True}

        with patch.dict(
            os.environ,
            {
                "UI_HARNESS_PROFILE": "org-admin",
                "UI_HARNESS_USERNAME": "local-admin@example.test",
                "UI_HARNESS_PASSWORD": "local-only-test-value",
            },
        ):
            result = login_profile("org-admin", capture)
        self.assertEqual(
            received,
            {
                "username": "local-admin@example.test",
                "password": "local-only-test-value",
            },
        )
        self.assertEqual(result["alias"], "org-admin")
        self.assertNotIn("local-only-test-value", json.dumps(result))

    def test_environment_credentials_must_be_supplied_as_a_pair(self):
        with patch.dict(
            os.environ, {"UI_HARNESS_USERNAME": "only-a-username"}, clear=False
        ):
            os.environ.pop("UI_HARNESS_PASSWORD", None)
            with self.assertRaisesRegex(ValueError, "Set both"):
                profile_metadata("viewer")

    def test_unknown_alias_fails_closed(self):
        with self.assertRaises(UnknownProfileError):
            profile_metadata("not-configured")
        called = False

        def should_not_run(*_args):
            nonlocal called
            called = True
            return {"authenticated": True}

        with self.assertRaises(UnknownProfileError):
            login_profile("not-configured", should_not_run)
        self.assertFalse(called)


class FakeHccTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "fake_hcc_server", HARNESS_DIR / "fake-hcc" / "server.py"
        )
        cls.fake_hcc = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cls.fake_hcc)

    def test_login_page_is_accessible_two_step_fixture(self):
        page = self.fake_hcc._LOGIN_PAGE
        for required in (
            'for="username"',
            'for="password"',
            "Continue",
            "Sign in",
            'role="status"',
            'aria-label="Console navigation"',
            "<h3>Subscriptions</h3>",
            "<h3>Inventory</h3>",
            "<h3>Organization settings</h3>",
            'id="configured-sign-in"',
            "Sign in with configured test account",
        ):
            self.assertIn(required, page)

    def test_fake_login_accepts_fixture_and_rejects_wrong_password(self):
        result = login_profile(
            "org-admin",
            lambda username, password: self.fake_hcc.authenticate(username, password)[
                1
            ],
        )
        self.assertEqual(result["alias"], "org-admin")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["auth_context"], "Organization administrator")

        status, failure = self.fake_hcc.authenticate(
            "admin@example.test", "wrong-value"
        )
        self.assertEqual(status, 401)
        self.assertEqual(failure, {"authenticated": False, "error": "Sign-in failed"})
        self.assertNotIn("wrong-value", json.dumps(failure))

    def test_configured_login_uses_environment_credentials_without_returning_them(self):
        with patch.dict(
            os.environ,
            {
                "UI_HARNESS_PROFILE": "org-admin",
                "UI_HARNESS_USERNAME": "local-admin@example.test",
                "UI_HARNESS_PASSWORD": "local-only-test-value",
            },
        ):
            status, result = self.fake_hcc.authenticate_configured("org-admin")
            wrong_status, wrong_profile = self.fake_hcc.authenticate_configured(
                "viewer"
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["alias"], "org-admin")
        self.assertNotIn("local-admin@example.test", json.dumps(result))
        self.assertNotIn("local-only-test-value", json.dumps(result))
        self.assertEqual(wrong_status, 403)
        self.assertFalse(wrong_profile["authenticated"])

    def test_server_is_loopback_only(self):
        with self.assertRaises(ValueError):
            self.fake_hcc.FakeHccServer(("0.0.0.0", 0))


class InstanceConfigTests(unittest.TestCase):
    def test_instance_references_ui_test_workflow_and_browser(self):
        config = (INSTANCE_DIR / "agent" / "instance.yaml").read_text(encoding="utf-8")
        self.assertIn("./workflows/ui-test", config)
        self.assertIn("- browser", config)

    def test_standard_targets_are_defined(self):
        targets = json.loads(
            (INSTANCE_DIR / "agent" / "targets.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(targets["default_urls"]), {"dev", "stage", "prod"})
        self.assertTrue(all(targets["default_urls"].values()))


if __name__ == "__main__":
    unittest.main()
