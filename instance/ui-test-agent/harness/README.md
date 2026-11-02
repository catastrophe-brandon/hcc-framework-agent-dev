# UI testing agent local harness

This harness gives contributors a repeatable, secret-free way to check the UI
testing instance before deploying it. It uses `uv` to create an isolated Python
environment from this directory's `pyproject.toml` and `uv.lock`. The default
checks use only Python and synthetic Jira, memory, and login data. They do not
load `.env`, call Jira, start the agent model, read Vault/Kubernetes secrets,
or contact a HCC target. Make sure Python 3.11+ and `uv` are available on
`PATH`; the harness scripts create and use their own virtual environment.

## Run offline checks

From the repository root:

```sh
./instance/ui-test-agent/harness/run-offline.sh
```

The first run creates the harness virtual environment. No third-party packages
are needed for this check.

The tests exercise the actual UI-test preflight script with in-process Jira and
memory fakes, verify ticket selection and `/retest` dispatch, check the fake
credential-profile boundary, and check the local fake HCC page and login
handler. They bind no sockets. Test state lives in memory and is discarded
when the command exits.

To run the checks directly:

```sh
python3 -m unittest discover \
  -s instance/ui-test-agent/harness/tests -v
```

## Ruff formatting and lint

Ruff is in the harness `dev` dependency group. From the repository root, check
all Python files in this instance with:

```sh
uv run --project instance/ui-test-agent/harness \
  ruff check --config instance/ui-test-agent/harness/pyproject.toml \
  instance/ui-test-agent
uv run --project instance/ui-test-agent/harness \
  ruff format --check --config instance/ui-test-agent/harness/pyproject.toml \
  instance/ui-test-agent
```

Run `ruff format` without `--check` to apply formatting. The configured lint
rules check common Python errors and import order; E501 is ignored because
fixture HTML and prompt strings have content-oriented line breaks.

## Fixtures

- `fake_services.py` provides narrow, stateful Jira and memory fakes for the
  offline preflight contract. Jira supports search, issue reads, and comments;
  unsupported operations (including status changes) fail closed.
- `fake_mcp_server.py` exposes those fakes as optional loopback MCP services.
  Each process starts with a fresh synthetic issue or empty memory.
- `fixtures/jira-scenarios.json` holds the synthetic Jira issue fields and scenario
  text used by model cycles. Add or revise a scenario there; the runner reads the
  available Stage scenario names from the same file.
- `fake_auth.py` resolves only the fixture aliases `viewer` and `org-admin`.
  `login_profile()` passes dummy credentials directly to a supplied login
  callback and returns display metadata only. Callers cannot retrieve a
  credential through the public helper interface.
- `fake_hcc/server.py` serves a two-step accessible login and a small local
  console flow. It binds to loopback and accepts only fixture credentials.

To view the fixture in a browser, start it separately:

```sh
python3 instance/ui-test-agent/harness/fake-hcc/server.py
```

Then open `http://127.0.0.1:8765`. Stop it with Ctrl+C. The default offline
suite tests the same page and authentication handler without binding a socket.

For an optional real Chromium login smoke check, keep the fixture server
running and use a second terminal with Node 22+ and Chromium:

```sh
CHROME_BIN=/path/to/chromium node \
  instance/ui-test-agent/harness/browser-login-smoke.mjs viewer
```

The smoke check drives Chromium directly over CDP, verifies the two-step flow,
checks that a wrong password is rejected, confirms that successful login
clears the password field, and opens Subscriptions. It uses only the dummy profile and loopback;
it does not run the agent or Chrome DevTools MCP. It prints the alias and
pass/fail state, never the username or password.

The fixture profile file contains intentionally fake values. Never copy real
credentials into this directory. The fake login endpoint and browser page are
only suitable for local harness use.

## Verify Stage sign-in without a model cycle

To check the real Stage login helper and its signed-in marker without starting
Jira, memory, or the model, install the Node dependencies once and run:

```sh
npm ci --prefix instance/ui-test-agent/agent/login-helper
op run --env-file "$HOME/env/claude.env" -- \
  ./instance/ui-test-agent/harness/run-stage-login-smoke.sh
```

The environment file must provide `UI_TEST_USERNAME` and `UI_TEST_PASSWORD`.
You can also export those variables before running the script directly. It
starts a temporary Chromium profile with the Stage proxy, submits credentials
at most once, and passes only when the signed-in `User Avatar` control is
visible. It prints the helper's status and removes the temporary browser
profile on exit. It does not create a Jira report or test plan.

To use a local test account instead of the dummy fixture values, set
`UI_HARNESS_USERNAME` and `UI_HARNESS_PASSWORD`. Both must be set. The selected
alias defaults to `viewer` and can be changed with `UI_HARNESS_PROFILE=org-admin`;
the override applies only to that alias. The fake server and browser smoke read
these variables locally. Keep them out of ticket text, model prompts, reports,
and memory records.

To start the MCP fixtures, use two terminals:

```sh
./instance/ui-test-agent/harness/run-fake-mcp.sh jira
./instance/ui-test-agent/harness/run-fake-mcp.sh memory
```

These commands use the optional `servers` dependency set managed by `uv`;
there is no need to activate a virtual environment manually.

The default endpoints are `http://127.0.0.1:8766/mcp` and
`http://127.0.0.1:8767/mcp`. The memory fixture also serves the read-only
`/api/tasks` endpoint used by preflight capacity checks. Do not point an agent
at shared Jira or memory services when using this fixture.

## Run one model-driven cycle

Stage is the default target. The runner needs Chromium, `uv`, Node.js 22+, and
Chrome DevTools MCP. The runner syncs the separate `dev-bot` Python environment
from its lockfile before each cycle, keeping its bundled Claude CLI current. It
uses a temporary copy of the runner and bounds the main loop to one cycle, so
the harness does not depend on a particular `dev-bot` scheduled-source mode.
Install the stage login helper dependencies and Chrome DevTools MCP once:

```sh
npm ci --prefix instance/ui-test-agent/agent/login-helper
npm install --global chrome-devtools-mcp
```

Configure the stage test account, then run one synthetic Jira issue:

```sh
export UI_TEST_USERNAME
export UI_TEST_PASSWORD
./instance/ui-test-agent/harness/run-agent-cycle.sh --target stage --profile org-admin --allow-model
```

For this team's direct Vertex setup, the runner reads only the model-provider
values from `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`)
when the values are not already set in the shell. It passes the configured
Google ADC file into the isolated model process. No Anthropic API key is needed.
The ADC file must be available at the standard gcloud path or through
`GOOGLE_APPLICATION_CREDENTIALS`. You can also run the command through `op` if
that is how you provide the UI test account credentials:

```sh
op run --env-file "$HOME/env/claude.env" -- \
  ./instance/ui-test-agent/harness/run-agent-cycle.sh \
  --profile org-admin --scenario dashboard-widgets --allow-model
```

The `insights-chrome-shell` scenario asks the agent to check the signed-in landing
page and masthead, including service navigation, All Services, search,
notifications, settings, sidebar, and favorites. It permits only a reversible
favorite-service change, which the agent must restore. This scenario supports comparison with
the existing `insights-chrome` UI tests; the harness does not execute that
repository's test suite.

The `dashboard-widgets` scenario asks the agent to explore the dashboard and
widget library, develop tester-ready cases for widget management and widget
capabilities, and record the layout changes it makes. The
`dashboard-widgets-report` scenario verifies the current dashboard and library,
then drafts a full Markdown report from recorded discovery notes without
mutating widgets. Omit `--scenario` to run the default sign-in/dashboard
request.

Use the non-secret persona alias with `--profile <alias>`; it defaults to
`viewer`. In stage mode, set it to the alias for the test account supplied through
`UI_TEST_USERNAME` and `UI_TEST_PASSWORD`; the visible account/org role should
still be checked in the console.
`UI_TEST_USERNAME` and `UI_TEST_PASSWORD` are read only by the local
stage-login helper. The helper accepts only the requested profile alias in a
JSON request body and checks it against the locally selected alias; it never
accepts credential values in the request. The reusable implementation lives
under `../agent/login-helper` and can instead read `<alias>.username` and
`<alias>.password` files from `UI_TEST_CREDENTIALS_DIR` when deployed. It uses
the Red Hat `@redhat-cloud-services/playwright-test-auth`
package against the harness's existing Chromium session, clears the sign-in
fields after the attempt when possible, and never returns credentials to the
model, bot process, Jira, report, or memory. The package blocks TrustArc
requests before the helper reloads Stage, then performs the standard Red Hat
SSO flow. The harness runs one helper process per cycle. The helper blocks rapid
repeat submissions for five minutes; deployment uses the same cooldown as the
default bot polling interval, then later cycles can retry without a reset hook.
A successful login clears the cooldown for later session checks. The helper reports
authentication only when the Insights Chrome `User Avatar` button is visible
after Stage loads. This is the same signed-in marker used by the
Insights Chrome Playwright auth tests. A redirect to Stage without that marker
is unconfirmed and fails the harness check. The agent still inspects the
visible account/org context; the helper does not establish the account's role.
For an exploratory run that should expose the post-login TrustArc prompt to
the agent, set `UI_HARNESS_SHOW_COOKIE_PROMPT=1`. The helper still blocks the
prompt during SSO, then removes that blocker and reloads the dashboard. Leave
it unset for the usual run.
MFA, CAPTCHA, or unsupported sign-in forms remain a reported blocker.

The browser alone uses `http://squid.corp.redhat.com:3128` for stage traffic;
Jira and memory stay on local fixtures. The stage issue is read-only and asks
the agent to authenticate and reach the HCC dashboard. The runner does not deploy
anything or connect to real Jira or shared memory. Do not use production
credentials or set this harness up for production. Model calls may incur cost.

Direct Vertex mode needs `CLAUDE_CODE_USE_VERTEX=true`,
`ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`, and Google ADC. An optional
`ANTHROPIC_VERTEX_BASE_URL` can select a custom endpoint. Do not set
`CLAUDE_CODE_SKIP_VERTEX_AUTH` for direct Vertex; that flag is only for a Vertex
proxy or gateway, where it must be paired with `ANTHROPIC_VERTEX_BASE_URL`.
Optionally set `UI_HARNESS_MODEL` to override the model in `dev-bot/config.json`.

For direct Vertex access, leave `ANTHROPIC_VERTEX_BASE_URL` unset, including
when using the standard `https://aiplatform.googleapis.com/` endpoint. Claude
Code derives that endpoint from `CLOUD_ML_REGION`; `/status` may still display
the standard URL when the variable is unset. Setting the URL explicitly in
`$HOME/env/claude.env` caused the Sonnet 5 availability error when commands ran
under `op run`, while unsetting it fixed the request. Reserve
`ANTHROPIC_VERTEX_BASE_URL` for an actual proxy or custom gateway.

If a Vertex model works without `op` but fails under `op run`, check whether
the env file sets this variable. To verify without editing the env file:

```sh
op run --env-file "$HOME/env/claude.env" -- \
  env -u ANTHROPIC_VERTEX_BASE_URL \
  claude --print --model claude-sonnet-5 "Reply with OK."
```

If that succeeds, remove `ANTHROPIC_VERTEX_BASE_URL` from the env file. Keep
the direct Vertex project, region, provider flag, and ADC configuration.

If your organization permits direct Anthropic API access, set
`ANTHROPIC_API_KEY` instead of the Vertex settings.

Use the entirely local simulated UI with `--target local --allow-model`. That
mode accepts only `viewer` and `org-admin`, and optional `UI_HARNESS_USERNAME`
plus `UI_HARNESS_PASSWORD` are fixture-only values. Never use real HCC
credentials in local mode.

The temporary bot workspace is deleted after the cycle. The printed results
directory is retained under `/tmp/ui-test-agent-results-*`; when the agent posts
a report, `jira-report.md` contains the readable Jira comment, while
`summary.json` keeps the structured harness checks. The directory also contains
fixture state, costs, logs, and the model transcript. The runner
scrubs configured UI credentials from persisted text files and records
whether it detected a leak. Stage result bundles omit screenshot attachments
so images of identity forms cannot retain entered values. The runner allows up
to 150 model turns by default; override with `--max-turns` (1–200) for a shorter
or bounded cycle. The default cycle timeout is 900 seconds; override with
`--timeout` when needed.

## What this does not verify

The offline tests do not launch Claude/Vertex, use Chrome DevTools MCP, or
assess the model's exploration and reporting quality. Use the opt-in cycle
above to exercise those parts. It must not reuse the developer `.env` or
connect real Jira, shared memory, production, or Squid for any purpose outside
the stage browser session. The model tools are not isolated by an OS-level
network sandbox; keep fixture scenarios trusted and local. Do not run the
normal bot launcher for this harness.

For harness goals and the planned full-cycle acceptance checks, see
[`../TEST-HARNESS-PLAN.md`](../TEST-HARNESS-PLAN.md).
