# UI Testing Agent — Local Harness Plan

## Goal

Let contributors check changes to the instance definition without deploying it or contacting real Jira. Deterministic checks use only fakes; the optional model cycle uses fake Jira and memory with stage as its default UI target.

## What has been checked so far

- The ticket preflight selected a synthetic Jira issue using in-process Jira and memory stubs.
- Headless Chromium completed a local two-step login using dummy credentials, driven directly over Chrome DevTools Protocol.
- Neither check ran the agent model or used the configured Chrome DevTools MCP. They are smoke checks, not an end-to-end agent run.
- Local Vertex troubleshooting (2026-10-13): the unwrapped Claude CLI succeeded with `claude-sonnet-5`, but the same request under `op run` failed as unavailable. The wrapper supplied `ANTHROPIC_VERTEX_BASE_URL=https://aiplatform.googleapis.com/`; unsetting only that variable made the request succeed. For direct Vertex, omit `ANTHROPIC_VERTEX_BASE_URL` and let Claude Code derive its standard endpoint from `CLOUD_ML_REGION`. `/status` can still show `https://aiplatform.googleapis.com/` when the variable is unset. See the troubleshooting note in `harness/README.md`.
- Full local model cycle (2026-10-13): after removing that env override, the runner completed `UITEST-LOCAL-1` against the loopback fixture with `claude-sonnet-5` on Vertex. Exit code 0; readiness `Ready`; all three cases passed; one report comment and one learning-memory record were written to local fixtures; internal task status became `done`; Jira issue stayed `To Do`; credential-leak check was false. Cost was about $0.32 over 23 turns. This validates the local model-driven cycle, not Stage. The report says screenshots covered four states, but the transcript records three screenshot calls (post-login home, Subscriptions, Inventory); it did not capture the sign-in form screenshot it claims.
- Stage attempts (2026-10-13): the first cycle safely blocked because `sso.stage.redhat.com` was missing from the sign-in host allowlist. After adding that host, the helper reported submission but SSO returned `Invalid Request`; the model retried the credentials three times before the cycle was stopped. No console flows were reached. A later cycle was inconclusive: the SSO cookie banner blocked the helper, then the model dismissed it and clicked through the username step itself; the helper blocked another password submission. No password was submitted in that later cycle, no application cases ran, and the Jira/memory writes were only to local fixtures. The run flagged a username disclosure in persisted text, which was scrubbed; its screenshot attachment was removed. The local helper now uses `@redhat-cloud-services/playwright-test-auth` to block TrustArc requests before reloading Stage and perform the standard SSO login, limits the cycle to one login attempt, and tells the model to stop on a helper blocker.
- Stage model cycle (2026-10-13): `claude-sonnet-5` authenticated through the Playwright auth package, confirmed the account context, explored Subscriptions and Insights Inventory, and posted one report plus a learning-memory record to local fixtures. The report contained six passing cases and readiness `Ready`; Inventory was an expected empty state for this account, while a non-blocking analytics consent request and app-shell console warnings were recorded. The console showed `Org. Administrator` even though the runner label was `viewer`; the label does not verify SSO identity or privileges, so viewer-role coverage remains unverified. No mutations were performed. Harness exit code was 0 and the credential-leak check was false. Stage screenshots were removed from the result bundle. This validates the local Stage harness cycle, not a deployed agent or real Jira integration.
- Stage dashboard-plan cycle (2026-10-14): changed the synthetic Stage ticket to sign-in plus HCC dashboard landing. The default TrustArc blocker suppressed the consent prompt, so the run did not cover it. Added `UI_HARNESS_SHOW_COOKIE_PROMPT=1` for this exploratory cycle: the login helper removed its TrustArc route after SSO and reloaded the dashboard. The agent independently dismissed the visible cookie banner and stated that it had done so, but the Jira report omitted the banner from its three test cases and their steps. Readiness was `Ready`; report and memory were local fixtures, screenshots were removed, and credential-leak check was false. This shows the model discovers and handles the banner, while its exploration findings are not consistently carried into the written plan.

## Implementation status

The contributor harness includes a no-network offline suite, fake Jira and
memory services, a local console fixture, and an opt-in one-cycle model runner.
Stage is the default target: the runner starts fake Jira and memory, opens stage
in Chromium through Squid, and provides an isolated local login helper for the
environment-supplied test account. The model receives no login credentials.
`--target local` selects the simulated console fixture. Reports, fixture
state, costs, logs, and transcript are retained in a temporary results
directory. Model use requires an explicit endpoint setup and `--allow-model`.

The offline suite exercises preflight and fixture behavior without starting
the bot or model. Full local-fixture and Stage UI cycles have completed against
a live model endpoint. These runs validate the local harness and workflow, not
a deployed agent or real Jira integration.

## Two run modes

### Offline checks — no model access

Run deterministic checks for target/config validation, preflight selection and retest dispatch, fake service behavior, and the local login fixture. These should work without Jira, Vertex/Claude credentials, Vault access, or a deployment.

### One-cycle agent run — optional model access

Run the actual runner for one cycle against fake Jira and memory. Stage is the default UI target; this exercises the workflow prompt, loopback login endpoint, browser proxy, and local credential helper against Stage. It requires explicitly configured model endpoint and stage test-account environment variables plus `--allow-model`; it must not load the repository `.env` or connect to real Jira or shared memory. Model usage may incur cost, so the runner clearly announces the target before starting.

## Implemented layout

```text
harness/
  fake-auth/profiles.json
  fake-hcc/server.py
  fake_auth.py
  fake_mcp_server.py
  fake_services.py
  run-offline.sh
  run-agent-cycle.sh
  run_agent_cycle.py
  tests/

agent/login-helper/
  browser-login.mjs
  server.py
  package.json
  package-lock.json
```

All fixture services and run state are isolated under the harness or a temporary
directory. Fake Jira and memory share `fake_services.py` and are exposed over
loopback by `fake_mcp_server.py`. The optional runner uses a temporary dev-bot
copy, local Jira/memory fixtures, and either loopback HCC or Stage through
Squid; it bounds that copy to one cycle without relying on a scheduled-source
mode. The reusable Stage login helper lives with the agent implementation,
exposes a loopback HTTP endpoint, and resolves the requested alias inside the
helper process. The model calls the endpoint through the already allowed
`Bash` tool.

## Scenarios

- No labeled issue: preflight skips without starting an agent cycle.
- One labeled issue: the Stage-default cycle reads a synthetic ticket and explores Stage; explicit `--target local` exercises the local fake console.
- Valid alias: selected profile reaches the matching fake identity/org; report names the alias only.
- Missing or unknown alias: agent asks for clarification or pauses; it never silently falls back to a different profile.
- Login failure: result is blocked or inconclusive; the agent does not claim the requested UI flow passed.
- Successful flow: the fake Jira receives a structured report comment; issue status remains unchanged; fake memory stores the run.
- Retest: `/retest` creates a distinct second run and preserves the first run's memory record.
- Prompt injection: malicious-looking text in the synthetic ticket or page is treated as data and does not override the workflow.

## Safety requirements

- Use only loopback URLs, fixture accounts, and dummy passwords in offline and local modes.
- The agent process must receive local Jira and memory URLs explicitly in every one-cycle mode.
- Do not load `.env` or read Vault/Kubernetes secrets. In stage mode, pass the two test-account environment variables only to the local login helper, not to the bot/model process.
- In stage one-cycle mode, configure Jira and memory to loopback fixtures and route only the browser's UI traffic through the configured Squid proxy. The explicitly configured model endpoint is also used.
- Never include dummy or real passwords in model prompts, tool arguments, transcripts, Jira reports, screenshots, logs, or memory.
- Save screenshots and run artifacts only under the printed, unique temporary results directory. Keep that directory for review; clean up the disposable workspace and local services when the cycle exits.
- The runner does not create an OS-level network sandbox for model tools. Keep model-cycle scenarios trusted.

## Acceptance checks

- A contributor can run offline checks with the documented command and no secret setup.
- The fake Jira and memory services receive all expected calls and no external calls.
- The one-cycle runner exits after one synthetic issue, uses fake Jira and memory, and opens the stage target through Squid (or loopback UI in local mode).
- Stage credentials are entered by the local helper and do not appear in model prompts, Jira, reports, memory, or persisted artifacts.
- A Stage login counts as successful only when the helper sees the signed-in Insights Chrome `User Avatar` button; a redirect without that marker fails the cycle check.
- Missing/invalid aliases and failed login produce a safe blocked outcome.
- Retest creates a new run without overwriting prior results.
- Cleanup stops local services and leaves no tracked files or persistent test state behind.

## Current gaps

The repository now contains the alias-aware helper, its instance-specific
sidecar template, a browser-only proxy setting, and direct helper unit tests.
It still needs an App Interface resource, a dedicated Vault-backed Secret in
the selected deployment namespace, and live validation of Stage access and
account roles. Local Stage cycles use fake Jira/memory and do not validate real
Jira or deployed network configuration. Contributors need Chromium, Chrome
DevTools MCP, model credentials, and a Stage test account in
`UI_TEST_USERNAME`/`UI_TEST_PASSWORD`. Local fixture mode uses
`UI_HARNESS_USERNAME`/`UI_HARNESS_PASSWORD` and does not test real SSO.
