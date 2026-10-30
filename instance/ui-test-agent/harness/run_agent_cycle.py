"""Run one UI-testing agent cycle against disposable loopback fixtures."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARNESS_DIR.parents[2]
DEV_BOT_DIR = REPO_ROOT / "dev-bot"
INSTANCE_AGENT_DIR = REPO_ROOT / "instance" / "ui-test-agent" / "agent"
LOGIN_HELPER_DIR = INSTANCE_AGENT_DIR / "login-helper"
INSTANCE_ID = "ui-test-harness"
LABEL = "ui-test"
TARGET_URLS = {
    "stage": "https://console.stage.redhat.com",
    "local": "http://127.0.0.1:8765",
}
PORTS = {
    "hcc": 8765,
    "jira": 8766,
    "memory": 8767,
    "login": 8768,
    "chrome": 9222,
    "metrics": 9091,
}
MODEL_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "UI_HARNESS_MODEL",
    "CLOUD_ML_REGION",
    "VERTEX_LOCATION",
)


VERTEX_MODEL_ENV_PREFIXES = ("VERTEX_REGION_CLAUDE_",)


def _claude_settings_environment(parent: dict[str, str]) -> dict[str, str]:
    """Load only model-provider settings from the user's Claude settings file."""
    home = Path(parent.get("HOME") or Path.home())
    config_dir = Path(parent.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
    settings_path = config_dir / "settings.json"
    if not settings_path.is_file():
        return {}

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read Claude Code settings at {settings_path}: {exc}"
        ) from exc

    env = settings.get("env", {}) if isinstance(settings, dict) else {}
    if not isinstance(env, dict):
        raise RuntimeError(
            f"Claude Code settings at {settings_path} has an invalid 'env' section"
        )

    allowed = set(MODEL_ENV_VARS) - {"ANTHROPIC_API_KEY"}
    return {
        name: value
        for name, value in env.items()
        if isinstance(name, str)
        and isinstance(value, str)
        and (
            name in allowed
            or any(name.startswith(prefix) for prefix in VERTEX_MODEL_ENV_PREFIXES)
        )
    }


def _merged_model_settings(parent: dict[str, str]) -> dict[str, str]:
    """Use shell values over the corresponding values in Claude user settings."""
    settings = _claude_settings_environment(parent)
    settings.update(
        {name: parent[name] for name in MODEL_ENV_VARS if parent.get(name, "").strip()}
    )
    for name in MODEL_ENV_VARS:
        if name in settings:
            settings[name] = settings[name].strip()
    return settings


def _vertex_adc_path(parent: dict[str, str], settings: dict[str, str]) -> str | None:
    configured_path = settings.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if configured_path:
        credential_file = Path(configured_path).expanduser()
        if not credential_file.is_file() or not os.access(credential_file, os.R_OK):
            return None
        return str(credential_file.resolve())

    home = Path(parent.get("HOME") or Path.home())
    default_adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    if default_adc.is_file() and os.access(default_adc, os.R_OK):
        return str(default_adc.resolve())
    return None


def _model_environment(parent: dict[str, str]) -> dict[str, str]:
    settings = _merged_model_settings(parent)
    use_vertex = settings.get("CLAUDE_CODE_USE_VERTEX", "").lower()
    skip_vertex_auth = settings.get("CLAUDE_CODE_SKIP_VERTEX_AUTH", "").lower()

    if use_vertex not in {"1", "true"}:
        if use_vertex and use_vertex not in {"0", "false"}:
            raise RuntimeError("CLAUDE_CODE_USE_VERTEX must be 'true' or '1'")
        if any(
            settings.get(name)
            for name in (
                "CLAUDE_CODE_SKIP_VERTEX_AUTH",
                "ANTHROPIC_VERTEX_BASE_URL",
                "ANTHROPIC_VERTEX_PROJECT_ID",
                "CLOUD_ML_REGION",
            )
        ):
            raise RuntimeError(
                "Vertex settings were found, but CLAUDE_CODE_USE_VERTEX is not "
                "enabled. Set it to true in the shell or Claude settings."
            )
        if settings.get("ANTHROPIC_API_KEY"):
            api_names = {
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
                "UI_HARNESS_MODEL",
            }
            return {name: settings[name] for name in api_names if settings.get(name)}
        raise RuntimeError(
            "No model provider is configured. Set Vertex settings in the shell or "
            "the 'env' section of ~/.claude/settings.json, or set ANTHROPIC_API_KEY "
            "if direct Anthropic API access is permitted."
        )

    if skip_vertex_auth and skip_vertex_auth not in {"1", "true", "0", "false"}:
        raise RuntimeError("CLAUDE_CODE_SKIP_VERTEX_AUTH must be 'true' or '1'")

    project = settings.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    if not project:
        raise RuntimeError(
            "Vertex configuration is missing ANTHROPIC_VERTEX_PROJECT_ID. Set it "
            "in the shell or in the 'env' section of ~/.claude/settings.json."
        )

    vertex_names = {
        name
        for name in MODEL_ENV_VARS
        if name not in {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"}
        and settings.get(name)
    }
    for name in VERTEX_MODEL_ENV_PREFIXES:
        vertex_names.update(
            key for key in settings if key.startswith(name) and settings.get(key)
        )
    vertex_environment = {name: settings[name] for name in vertex_names}

    if skip_vertex_auth in {"1", "true"}:
        if not settings.get("ANTHROPIC_VERTEX_BASE_URL"):
            raise RuntimeError(
                "Vertex proxy mode is missing ANTHROPIC_VERTEX_BASE_URL. "
                "CLAUDE_CODE_SKIP_VERTEX_AUTH is only needed for a proxy or gateway."
            )
        return vertex_environment

    if not settings.get("CLOUD_ML_REGION"):
        raise RuntimeError(
            "Direct Vertex configuration is missing CLOUD_ML_REGION. Set it in the "
            "shell or in the 'env' section of ~/.claude/settings.json."
        )

    adc_path = _vertex_adc_path(parent, settings)
    if not adc_path:
        raise RuntimeError(
            "Direct Vertex needs Google Application Default Credentials. Set "
            "GOOGLE_APPLICATION_CREDENTIALS to a readable credential file or run "
            "'gcloud auth application-default login'. The harness passes the ADC "
            "file path into its isolated model environment."
        )
    vertex_environment["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path
    return vertex_environment


def _check_ports_free(target: str):
    busy = []
    names = ("jira", "memory", "chrome", "metrics")
    if target == "local":
        names += ("hcc",)
    else:
        names += ("login",)
    for name in names:
        port = PORTS[name]
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(f"{name} ({port})")
    if busy:
        raise RuntimeError("Harness ports already in use: " + ", ".join(busy))


def _python_runtime() -> Path:
    python = DEV_BOT_DIR / ".venv" / "bin" / "python"
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv to sync the dev-bot Python environment.")
    print("Syncing the dev-bot Python environment from uv.lock...", flush=True)
    subprocess.run(
        [
            uv,
            "sync",
            "--project",
            str(DEV_BOT_DIR),
            "--extra",
            "dev",
            "--no-install-workspace",
        ],
        check=True,
    )
    if not python.is_file():
        raise RuntimeError("uv did not create dev-bot/.venv/bin/python")
    required = "import claude_agent_sdk, mcp.server.fastmcp, uvicorn"
    check = subprocess.run(
        [str(python), "-c", required], capture_output=True, text=True
    )
    if check.returncode != 0:
        raise RuntimeError(
            "The synchronized dev-bot environment is missing required packages: "
            + (check.stderr.strip() or check.stdout.strip())
        )
    return python


def _find_chromium() -> str:
    candidates = [os.environ.get("CHROME_BIN", "")]
    candidates.extend(
        shutil.which(name) or ""
        for name in ("chromium", "chromium-browser", "google-chrome")
    )
    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/opt/homebrew/bin/chromium",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise RuntimeError(
        "Could not find Chromium. Set CHROME_BIN to its executable path."
    )


def _check_stage_login_dependencies(node: str) -> None:
    """Fail before starting a model cycle if the local SSO packages are absent."""
    check = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "await import('playwright'); "
            "await import('@redhat-cloud-services/playwright-test-auth')",
        ],
        cwd=LOGIN_HELPER_DIR,
        capture_output=True,
        text=True,
    )
    if check.returncode:
        raise RuntimeError(
            "Stage login helper dependencies are missing or failed to load. "
            "Run `npm ci --prefix instance/ui-test-agent/agent/login-helper` from the repository root."
        )


def _limit_runtime_to_one_cycle(runtime: Path) -> None:
    """Bound the copied dev-bot runner to one prompt and no scheduled sleep."""
    # The harness needs one bounded model cycle per invocation, while upstream
    # dev-bot only exposes its normal continuous loop. Patch the disposable copy
    # so a local harness run exits cleanly without changing the checked-out source.
    runner_module = runtime / "bot" / "run.py"
    runner_source = runner_module.read_text(encoding="utf-8")
    cycle_loop = "            while True:"
    if runner_source.count(cycle_loop) != 1:
        raise RuntimeError(
            "Could not locate the dev-bot main cycle loop to limit the harness run"
        )
    runner_source = runner_source.replace(
        cycle_loop,
        "            for _cycle in range(1):",
        1,
    )
    sleep_call = "    time.sleep(sleep_seconds)"
    if runner_source.count(sleep_call) != 1:
        raise RuntimeError("Could not locate the dev-bot cycle sleep for the harness")
    # The normal worker waits between cycles, including after preflight errors;
    # this disposable one-cycle run should return its result without that delay.
    runner_source = runner_source.replace(sleep_call, "    return 0", 1)
    runner_module.write_text(runner_source, encoding="utf-8")


def _write_runtime(
    workspace: Path, model: dict[str, str], max_turns: int, timeout: int, target: str
):
    runtime = workspace / "dev-bot"
    runtime.mkdir()
    for name in ("bot", "presets", ".claude"):
        shutil.copytree(DEV_BOT_DIR / name, runtime / name)
    for name in ("config.json", "project-repos.json"):
        source = DEV_BOT_DIR / name
        if source.is_file():
            shutil.copy2(source, runtime / name)

    _limit_runtime_to_one_cycle(runtime)

    config_path = runtime / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["claude"]["model"] = model.get("UI_HARNESS_MODEL", config["claude"]["model"])
    config["claude"]["maxTurns"] = max_turns
    config["claude"]["cycleTimeoutSeconds"] = max(30, timeout - 15)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    chrome_command = shutil.which("chrome-devtools-mcp")
    if not chrome_command:
        raise RuntimeError(
            "Install Chrome DevTools MCP once with: npm install --global chrome-devtools-mcp"
        )
    command, args = chrome_command, []
    args.extend(["--browserUrl", "http://127.0.0.1:9222", "--acceptInsecureCerts"])
    mcp_config = {
        "mcpServers": {
            "chrome-devtools": {"command": command, "args": args},
            "bot-memory": {"type": "http", "url": "http://127.0.0.1:8767/mcp"},
        }
    }
    (runtime / ".mcp.json").write_text(
        json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8"
    )

    config_repo = workspace / "config-repo"
    profile = config_repo / "ui-test-agent"
    profile.mkdir(parents=True)
    shutil.copytree(INSTANCE_AGENT_DIR, profile / "agent")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git is required to create the disposable config overlay")
    subprocess.run(
        [git, "init", "-q", str(config_repo)], check=True, capture_output=True
    )
    subprocess.run(
        [git, "-C", str(config_repo), "config", "user.name", "UI Harness"], check=True
    )
    subprocess.run(
        [git, "-C", str(config_repo), "config", "user.email", "ui-harness@localhost"],
        check=True,
    )
    subprocess.run([git, "-C", str(config_repo), "add", "ui-test-agent"], check=True)
    subprocess.run(
        [git, "-C", str(config_repo), "commit", "-qm", "Create local one-cycle config"],
        check=True,
    )
    return runtime, config_repo


def _base_environment(home: Path, python: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join([str(python.parent), os.environ.get("PATH", "")]),
        "HOME": str(home),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "LANG": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


def _start(
    name: str, command: list[str], cwd: Path, env: dict[str, str], artifacts: Path
):
    log = (artifacts / f"{name}.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process, log


def _http_json(url: str, method="GET", body=None, timeout=2):
    opener = build_opener(ProxyHandler({}))
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(
        url, data=payload, method=method, headers={"Content-Type": "application/json"}
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def _wait_for_http(url: str, process: subprocess.Popen, name: str, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"{name} exited while starting; inspect its log in the results directory"
            )
        try:
            _http_json(url)
            return
        except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            time.sleep(0.2)
    raise RuntimeError(
        f"Timed out starting {name}; inspect its log in the results directory"
    )


def _stop(processes: list[subprocess.Popen]):
    for process in reversed(processes):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)


def _redact(value, secrets: list[str]):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _capture_state(artifacts: Path, secrets: list[str], target: str) -> dict:
    state = {}
    endpoints = {
        "jira": "http://127.0.0.1:8766/__harness/state",
        "memory": "http://127.0.0.1:8767/__harness/state",
    }
    if target == "local":
        endpoints["hcc"] = "http://127.0.0.1:8765/__harness/state"
    else:
        endpoints["stage_login"] = "http://127.0.0.1:8768/__harness/state"
    for name, url in endpoints.items():
        try:
            state[name] = _http_json(url)
        except Exception as exc:
            state[name] = {"capture_error": type(exc).__name__}

    serialized = json.dumps(state)
    credential_leak = any(secret and secret in serialized for secret in secrets)
    state = _redact(state, secrets)
    (artifacts / "state.json").write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )
    return {"state": state, "credential_leak_detected": credential_leak}


def _sanitize_artifacts(artifacts: Path, secrets: list[str]) -> bool:
    """Redact fixture credentials from persisted text artifacts."""
    leaked = False
    text_suffixes = {".json", ".jsonl", ".log", ".md", ".txt"}
    for path in artifacts.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(secret and secret in original for secret in secrets):
            leaked = True
            path.write_text(_redact(original, secrets), encoding="utf-8")
    return leaked


def _remove_stage_screenshots(artifacts: Path) -> None:
    """Avoid retaining images that may contain entered identity values."""
    for path in artifacts.rglob("*"):
        if path.is_file() and path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            path.unlink()


def _extract_report(jira_state: dict) -> tuple[list[dict], str | None]:
    reports = []
    for issue in jira_state.get("issues", {}).values():
        comments = issue.get("comments", [])
        for comment in comments:
            body = comment.get("body", "")
            if isinstance(body, str) and "UI Testing Agent Report" in body:
                reports.append({"issue_key": issue.get("key"), "body": body})
    readiness = None
    if reports:
        match = re.search(
            r"UI test report\s*[—-]\s*(Ready with risks|Ready|Not ready|Inconclusive)",
            reports[-1]["body"],
            re.I,
        )
        readiness = match.group(1) if match else None
    return reports, readiness


def _stage_login_authenticated(attempts: list[dict]) -> bool:
    return any(
        attempt.get("status") in {"authenticated", "already_authenticated"}
        for attempt in attempts
    )


def _cycle_passed(
    *,
    target: str,
    runner_exit: int,
    report_is_valid: bool,
    fixture_authenticated: bool,
    stage_login_attempts: list[dict],
    learned: bool,
    credential_leak: bool,
) -> bool:
    authenticated = (
        fixture_authenticated
        if target == "local"
        else _stage_login_authenticated(stage_login_attempts)
    )
    return (
        runner_exit == 0
        and report_is_valid
        and authenticated
        and learned
        and not credential_leak
    )


def _extract_agent_model_error(log_path: Path) -> str | None:
    if not log_path.is_file():
        return None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(r"\[agent\]\s+(.*)", line)
        if not match:
            continue
        message = match.group(1).strip()
        if re.search(r"\bmodel\b", message, re.I) and re.search(
            r"not available|not exist|no access|not have access|issue with the selected",
            message,
            re.I,
        ):
            return message
    return None


def _available_jira_scenarios() -> tuple[str, ...]:
    fixture_path = HARNESS_DIR / "fixtures" / "jira-scenarios.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    return tuple(
        scenario for scenario in fixtures["scenarios"] if scenario != "local-default"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-model", action="store_true", help="Confirm one external model cycle"
    )
    parser.add_argument(
        "--target",
        choices=tuple(TARGET_URLS),
        default=os.environ.get("UI_HARNESS_TARGET", "stage"),
        help="UI target mode (default: stage; choose local for the loopback fixture)",
    )
    parser.add_argument(
        "--profile", default=os.environ.get("UI_HARNESS_PROFILE", "viewer")
    )
    parser.add_argument(
        "--scenario",
        choices=_available_jira_scenarios(),
        default="auth-dashboard",
        help="Synthetic Jira request to run (default: auth-dashboard)",
    )
    parser.add_argument("--max-turns", type=int, default=150)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if not args.allow_model:
        parser.error("pass --allow-model to explicitly enable the model call")
    if args.max_turns < 1 or args.max_turns > 200:
        parser.error("--max-turns must be between 1 and 200")
    if args.timeout < 60:
        parser.error("--timeout must be at least 60 seconds")
    if args.target == "local" and args.profile not in {"viewer", "org-admin"}:
        parser.error("local target profile must be viewer or org-admin")
    if args.target == "stage" and not re.fullmatch(
        r"[A-Za-z0-9:_-]{1,80}", args.profile
    ):
        parser.error("stage --profile must be a non-secret profile alias")

    parent_env = dict(os.environ)
    if args.target == "stage":
        username = parent_env.get("UI_TEST_USERNAME", "")
        password = parent_env.get("UI_TEST_PASSWORD", "")
        missing_credentials = [
            name
            for name, value in (
                ("UI_TEST_USERNAME", username),
                ("UI_TEST_PASSWORD", password),
            )
            if not value
        ]
        if missing_credentials:
            parser.error(
                "stage mode is missing or has an empty value for: "
                + ", ".join(missing_credentials)
            )
        secrets = [username, password]
        node = shutil.which("node")
        if node is None:
            parser.error(
                "stage mode requires Node.js 22+ for the local credential helper"
            )
        _check_stage_login_dependencies(node)
    else:
        username = parent_env.get("UI_HARNESS_USERNAME", "")
        password = parent_env.get("UI_HARNESS_PASSWORD", "")
        if bool(username) != bool(password):
            missing_credential = (
                "UI_HARNESS_PASSWORD" if username else "UI_HARNESS_USERNAME"
            )
            parser.error(
                f"local mode has a partial fixture credential pair; "
                f"{missing_credential} is unset or empty"
            )
        secrets = [username, password]
    if args.target == "local" and not username and not password:
        profiles = json.loads(
            (HARNESS_DIR / "fake-auth" / "profiles.json").read_text(encoding="utf-8")
        )
        secrets.extend(
            [profiles[args.profile]["username"], profiles[args.profile]["password"]]
        )
    model_env = _model_environment(parent_env)
    _check_ports_free(args.target)
    chromium = _find_chromium()
    python = _python_runtime()

    started_at = datetime.now(UTC).isoformat()
    artifacts = Path(tempfile.mkdtemp(prefix="ui-test-agent-results-"))
    workspace = Path(tempfile.mkdtemp(prefix="ui-test-agent-work-"))
    processes: list[subprocess.Popen] = []
    log_handles = []
    print(
        f"Starting one {args.target} model cycle. Jira and memory use local fixtures; "
        f"the browser target is {TARGET_URLS[args.target]}.",
        flush=True,
    )
    print(f"Results directory: {artifacts}", flush=True)

    try:
        config_repo = workspace / "config-repo"
        runtime, config_repo = _write_runtime(
            workspace, model_env, args.max_turns, args.timeout, args.target
        )
        agent_config = json.loads((runtime / "config.json").read_text(encoding="utf-8"))
        selected_model = agent_config["claude"]["model"]
        home = workspace / "home"
        home.mkdir()
        base_env = _base_environment(home, python)

        if args.target == "local":
            hcc_env = {
                **base_env,
                "UI_HARNESS_PROFILE": args.profile,
                "UI_HARNESS_USERNAME": username,
                "UI_HARNESS_PASSWORD": password,
            }
            hcc, log = _start(
                "hcc",
                [str(python), str(HARNESS_DIR / "fake-hcc" / "server.py")],
                REPO_ROOT,
                hcc_env,
                artifacts,
            )
            processes.append(hcc)
            log_handles.append(log)
            _wait_for_http("http://127.0.0.1:8765/__harness/state", hcc, "fake HCC")

        for service, port in (("jira", 8766), ("memory", 8767)):
            service_env = {**base_env}
            if service == "jira":
                service_env["UI_HARNESS_PROFILE"] = args.profile
                service_env["UI_HARNESS_TARGET"] = args.target
                service_env["UI_HARNESS_SCENARIO"] = args.scenario
            process, log = _start(
                service,
                [
                    str(python),
                    str(HARNESS_DIR / "fake_mcp_server.py"),
                    service,
                    "--port",
                    str(port),
                ],
                REPO_ROOT,
                service_env,
                artifacts,
            )
            processes.append(process)
            log_handles.append(log)
            _wait_for_http(
                f"http://127.0.0.1:{port}/__harness/state", process, f"fake {service}"
            )

        browser_home = workspace / "browser-home"
        browser_home.mkdir()
        browser_env = {
            "PATH": base_env["PATH"],
            "HOME": str(browser_home),
            "TMPDIR": base_env["TMPDIR"],
            "LANG": "C.UTF-8",
        }
        chrome_log = (artifacts / "chromium.log").open("w", encoding="utf-8")
        log_handles.append(chrome_log)
        chrome_proxy = (
            "http://squid.corp.redhat.com:3128"
            if args.target == "stage"
            else "direct://"
        )
        chrome_target = TARGET_URLS[args.target]
        chrome = subprocess.Popen(
            [
                chromium,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--disable-sync",
                "--disable-extensions",
                "--use-mock-keychain",
                "--disable-background-networking",
                f"--proxy-server={chrome_proxy}",
                "--proxy-bypass-list=localhost;127.0.0.1;::1",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222",
                "--remote-allow-origins=*",
                f"--user-data-dir={workspace / 'chromium-profile'}",
                chrome_target,
            ],
            cwd=workspace,
            env=browser_env,
            stdout=chrome_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(chrome)
        _wait_for_http("http://127.0.0.1:9222/json/version", chrome, "Chromium")

        if args.target == "stage":
            login_env = {
                **base_env,
                "UI_HARNESS_PROFILE": args.profile,
                "UI_TEST_USERNAME": username,
                "UI_TEST_PASSWORD": password,
                "UI_HARNESS_SHOW_COOKIE_PROMPT": parent_env.get(
                    "UI_HARNESS_SHOW_COOKIE_PROMPT", ""
                ),
            }
            login, log = _start(
                "stage-login",
                [str(python), str(LOGIN_HELPER_DIR / "server.py")],
                REPO_ROOT,
                login_env,
                artifacts,
            )
            processes.append(login)
            log_handles.append(log)
            _wait_for_http(
                "http://127.0.0.1:8768/__harness/state", login, "stage login helper"
            )

        bot_env = {
            **base_env,
            **model_env,
            "BOT_CONFIG_REPO": str(config_repo),
            "BOT_CONFIG_PATH": "ui-test-agent",
            "BOT_LABEL": LABEL,
            "BOT_INSTANCE_ID": INSTANCE_ID,
            "JIRA_MCP_URL": "http://127.0.0.1:8766/mcp",
            "BOT_MEMORY_URL": "http://127.0.0.1:8767/mcp",
            "MEMORY_API_URL": "http://127.0.0.1:8767/api",
            "COSTS_API_URL": "http://127.0.0.1:8767/api/costs",
            "CYCLE_RUNS_API_URL": "http://127.0.0.1:8767/api/cycle-runs",
            "BOT_DASHBOARD_URL": "http://127.0.0.1:8767/api/bot-status",
            "PLAYWRIGHT_BROWSERS_PATH": str(Path(chromium).parent),
        }
        uses_vertex = model_env.get("CLAUDE_CODE_USE_VERTEX", "").strip().lower() in {
            "1",
            "true",
        }
        vertex_proxy = model_env.get(
            "CLAUDE_CODE_SKIP_VERTEX_AUTH", ""
        ).strip().lower() in {"1", "true"}
        provider = (
            "Vertex proxy"
            if uses_vertex and vertex_proxy
            else "Vertex AI (ADC)"
            if uses_vertex
            else "Anthropic API"
        )
        print(f"Model provider: configured {provider}", flush=True)
        sdk_version = subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.metadata import version; print(version('claude-agent-sdk'))",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        print(
            f"Agent model: {selected_model}; Claude Agent SDK: {sdk_version}",
            flush=True,
        )

        runner_log = (artifacts / "runner.log").open("w", encoding="utf-8")
        log_handles.append(runner_log)
        # dev-bot exposes bot.run:main as a console script, not a -m entrypoint;
        # invoke that function because the harness skips installing the workspace.
        runner = subprocess.Popen(
            [
                str(python),
                "-c",
                "from bot.run import main; main()",
                "--label",
                LABEL,
                "--instance-id",
                INSTANCE_ID,
            ],
            cwd=runtime,
            env=bot_env,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(runner)
        try:
            runner_exit = runner.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(runner.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(runner.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                runner.wait(timeout=5)
            runner_exit = 124
        for handle in log_handles:
            handle.flush()

        bot_log = runtime / "data" / "bot.log"
        if bot_log.is_file():
            shutil.copy2(bot_log, artifacts / "bot.log")

        captured = _capture_state(artifacts, secrets, args.target)
        state = captured["state"]
        reports, readiness = _extract_report(state.get("jira", {}))
        agent_model_error = _extract_agent_model_error(artifacts / "runner.log")
        fixture_authenticated = any(
            event.get("alias") == args.profile
            for event in state.get("hcc", {}).get("login_events", [])
        )
        stage_login_attempts = state.get("stage_login", {}).get("attempts", [])
        stage_login_attempted = any(
            attempt.get("status") for attempt in stage_login_attempts
        )
        stage_login_authenticated = _stage_login_authenticated(stage_login_attempts)
        learned = any(
            item.get("external_key") == "UITEST-LOCAL-1"
            and item.get("source_type") == "jira"
            for item in state.get("memory", {}).get("memories", [])
        )
        snapshot = {
            "started_at": started_at,
            "runner_exit_code": runner_exit,
            "profile_alias": args.profile,
            "report_count": len(reports),
            "readiness": readiness,
            "agent_model_error": agent_model_error,
            "target_mode": args.target,
            "target_url": chrome_target,
            "login_events": state.get("hcc", {}).get("login_events", []),
            "stage_login_attempts": stage_login_attempts,
            "authenticated_as_expected_profile": (
                fixture_authenticated if args.target == "local" else None
            ),
            "stage_login_helper_invoked": stage_login_attempted,
            "stage_login_authenticated": stage_login_authenticated,
            "memory_record_created": learned,
            "credential_leak_detected": captured["credential_leak_detected"],
            "agent_report": reports[-1] if reports else None,
        }
        runtime_log = runtime / "data" / "bot.log"
        if runtime_log.is_file():
            shutil.copy2(runtime_log, artifacts / "bot.log")
        costs = runtime / "data" / "costs.jsonl"
        if costs.is_file():
            shutil.copy2(costs, artifacts / "costs.jsonl")
        transcript_dir = home / ".claude" / "projects"
        if transcript_dir.is_dir():
            shutil.copytree(
                transcript_dir, artifacts / "transcripts", dirs_exist_ok=True
            )

        credential_leak = captured["credential_leak_detected"] or _sanitize_artifacts(
            artifacts, secrets
        )
        if args.target == "stage":
            _remove_stage_screenshots(artifacts)
        snapshot["credential_leak_detected"] = credential_leak
        if reports:
            report_body = _redact(reports[-1]["body"], secrets)
            (artifacts / "jira-report.md").write_text(
                report_body.rstrip() + "\n", encoding="utf-8"
            )
        (artifacts / "summary.json").write_text(
            json.dumps(_redact(snapshot, secrets), indent=2) + "\n", encoding="utf-8"
        )
        report_is_valid = len(reports) == 1 and readiness is not None
        if len(reports) != 1:
            print(
                f"Expected exactly one Jira report, found {len(reports)}.",
                flush=True,
            )
        elif readiness is None:
            print(
                "The Jira report is missing a recognized readiness result.",
                flush=True,
            )
        passed = _cycle_passed(
            target=args.target,
            runner_exit=runner_exit,
            report_is_valid=report_is_valid,
            fixture_authenticated=fixture_authenticated,
            stage_login_attempts=stage_login_attempts,
            learned=learned,
            credential_leak=credential_leak,
        )
        print(
            f"Cycle {'completed' if passed else 'did not meet harness checks'}; readiness: {readiness or 'missing'}."
        )
        if args.target == "stage" and not stage_login_authenticated:
            statuses = sorted(
                {attempt.get("status", "unknown") for attempt in stage_login_attempts}
            )
            detail = ", ".join(statuses) if statuses else "helper not called"
            print(
                "Stage authentication was not confirmed by the signed-in User Avatar control; "
                f"helper status: {detail}.",
                flush=True,
            )
        if agent_model_error:
            print(f"Model error: {agent_model_error}")
        print(
            f"Inspect Jira report, tool calls, memory, logs, and transcript under {artifacts}"
        )
        return 0 if passed else 1
    finally:
        _stop(processes)
        for handle in log_handles:
            try:
                handle.close()
            except Exception:
                pass
        if artifacts.exists():
            _sanitize_artifacts(artifacts, secrets)
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Harness could not start: {exc}", file=sys.stderr)
        raise SystemExit(2)
