# UI Testing Agent

Jira-driven UI exploration and readiness reporting for Hybrid Cloud Console.

## Configuration

- `agent/instance.yaml` selects the custom Jira workflow and browser runtime.
- `agent/targets.json` sets the shared dev, stage, and prod targets and supports app-specific overrides. Ephemeral-environment tickets provide their own URL.
- Set `BOT_CONFIG_PATH=instance/ui-test-agent` and a stable, unique `BOT_INSTANCE_ID` for this deployment.
- Set `BOT_LABEL` to the dedicated Jira intake label. The initial proposed value is `hcc-ui-test`.
- Provide the deployment's `JIRA_MCP_URL`; the framework supplies shared memory and Chromium through the `browser` runtime environment.
- Store each test identity in Vault and expose it to Kubernetes under a stable profile alias. Tickets select the alias, never include credentials. The signed-in account determines its existing org; a separate org ID is not required.

The local Stage harness routes Chromium through `http://squid.corp.redhat.com:3128`. The deployment template sets a browser-only `UI_BROWSER_PROXY` to the existing `devbot-proxy`; it leaves the bot's Jira, memory, and model proxy settings unchanged. Stage reachability from the deployed pod still needs validation.

Production is read-only by default. Mutating checks require an in-scope ticket and the approved test account to be active in its existing test org. Multi-profile support requires a credential helper that authenticates by alias without exposing secret values to the agent. The workflow pauses if the alias is unavailable or the active account/org cannot be established safely.

The agent posts its report to the original Jira issue, does not transition Jira status, and tracks prior test runs in the shared memory server.

## Local configuration location

Use `BOT_CONFIG_PATH=instance/ui-test-agent` for this instance. The config root contains the `agent/` directory loaded by the runner.

See [PLANNING.md](PLANNING.md) for the readiness rubric, report structure, and implementation decisions.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the sidecar layout, credential Secret contract, and remaining deployment checks.

See [TEST-HARNESS-PLAN.md](TEST-HARNESS-PLAN.md) for the deployment-free local validation plan.

Copy the ready-to-use [Jira request template](JIRA-REQUEST-TEMPLATE.md) into new UI testing tickets. The agent can work from a rough description; requesters do not need to write formal acceptance criteria or test cases.

## Try the agent locally

The deployment-free [harness guide](harness/README.md) explains the offline checks and the optional one-cycle model run. Stage is the default target for that run; it uses synthetic Jira and memory services, with stage browser traffic routed through Squid. You provide the stage test account through `UI_TEST_USERNAME` and `UI_TEST_PASSWORD`; the local login helper resolves the selected alias and keeps those values out of the bot and model. The harness does not deploy the instance or contact real Jira.
