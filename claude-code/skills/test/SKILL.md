---
name: test
description: Check the Mezmo setup for Claude Code. Shows the Mezmo Governance configuration with the governance hook's last result, whether the Mezmo ledger is on with the ledger hook's last send, a check of its URL and records waiting to be resent, the telemetry settings with a test of the ingestion key against Mezmo's OpenTelemetry endpoint, and whether Claude Code connects to Mezmo's MCP server. Use when the user asks why a tool call was blocked by Mezmo Governance, whether Mezmo governance, telemetry or MCP works, or why Claude data is missing in Mezmo.
argument-hint: "[--check-only] [--signals traces logs metrics|--all-signals]"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" status*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" check*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_config.py" status*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_common.py" *)
---

# Test the Mezmo setup

Arguments: $ARGUMENTS

## Governance

1. Show the configuration, the latest session's counts, the hook's last result and the ledger:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" status
   ```

2. If the service account token comes from settings or the environment, check it. This asks Mezmo Governance about a random decision and sends the ledger an empty batch (`POST []`, answered `202` with `{"accepted": 0}`), and records nothing in either:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" check
   ```

   A key saved as a sensitive plugin option (in the macOS Keychain, or `~/.claude/.credentials.json`) can't be read back. In that case the last result shows whether the hook is working, or the user can supply a private `--key-file` to check a token directly. Never put token text in Bash arguments. Follow setup's private-file procedure: create with `mezmo_common.py create-key-file`, fill via local user entry or Write (not Bash), and pass `--key-file "<KEY_FILE>" --delete-key-file`. If interrupted or parsing/cleanup fails, use `mezmo_common.py delete-key-file "<KEY_FILE>"`. File input protects Bash arguments, not past chat or all filesystem/process exposure.

3. Explain the results using this table:

   | What you see | What to tell the user |
   | --- | --- |
   | Plugin configuration unavailable / state unknown | Report available settings and session results, but do not claim governance is off. Repair Claude Code's access to its plugin options; setup remains strict. |
   | Governance is off | Bash and MCP tool calls run without a check. Run `/mezmo:setup governance` to turn it on. |
   | Off right after the plugin was reinstalled or its marketplace changed | Reinstalling deletes the saved service account token. Run `/mezmo:setup governance` again. If tool calls are blocked with `HTTP 401` meanwhile, run `/reload-plugins` or start a new session first. |
   | Required but not configured | Every Bash and MCP tool call is blocked until governance is configured. Ask their administrator for the service account token. |
   | `HTTP 401` | Mezmo Governance rejected the service account token. Rerun `/mezmo:setup governance` with a current key. |
   | `ENOAGENT`, `EAGENTINACTIVE` or `EPOLICYUNASSIGNED` | The key is valid, but no active agent with a policy is registered for it. An administrator fixes this in Mezmo Governance. |
   | `approval still pending` | The policy sent the call to a person, and nobody decided in time. The decision is in Mezmo Governance. Approving it there lets the same call through when Claude retries it. |
   | `argument name 'max-results' isn't accepted` | The service only accepts plain identifiers as argument names. MCP tool calls with such names are blocked until the service accepts them. |
   | `Mezmo Governance is unavailable` | The machine can't reach the authorize URL. Check the URL, proxy, VPN or firewall. |

## Ledger

`status` above ends with the ledger: on or off and why, the URL it sends to, the ledger hook's last send, and how many records wait in its retry file. The last send, such as `Ledger: last send 16:14:40 UTC to api.mezmo.com, HTTP 202, 2 events`, proves the hooks themselves send, even when the token is a sensitive plugin option that `check` can't read. `check` sends the ledger the empty batch. Explain the results using this table:

| What you see | What to tell the user |
| --- | --- |
| `Ledger: last send ..., HTTP 202` | The ledger hook's last send worked: each tool call is recorded. |
| `FAIL: Ledger: last send ..., HTTP 401` (or `403`) | The ledger rejected the token: a wrong key, or a key for another account. Rerun `/mezmo:setup governance` with the right service account token. Records wait in the retry file meanwhile. |
| `FAIL: Ledger: last send ... failed` | The hook couldn't reach the ledger. Check the API URL, proxy, VPN or firewall. |
| `FAIL: Ledger: last send ..., HTTP <other>` | Anything but `202` is a failure. Check the API URL in `/plugin configure mezmo@mezmo`. |
| `Ledger: no send recorded yet` | No session has sent anything since setup. In a new session, ask Claude to run `printf 'mezmo-onboarding-test\n'`, then run `/mezmo:test` again. |
| On, and the check says the ledger accepted the token | Each Bash and MCP tool call is recorded in the Mezmo ledger under the token's account. |
| Off: no service account token | The ledger uses the governance token. Run `/mezmo:setup governance`. |
| Off: the `ledger_enabled` option is off | The user turned it off. `/plugin configure mezmo@mezmo` turns it back on. |
| `rejected the service account token (HTTP 401)` | Rerun `/mezmo:setup governance` with a current token. Records wait in the retry file and are resent once the token works. |
| `Unexpected answer` or `HTTP 404` | The API URL has no ledger. Check the API URL in `/plugin configure mezmo@mezmo`. |
| Records waiting in the retry file | The ledger was unreachable or refused them. Later tool calls resend them, up to 10 at a time; the file keeps at most 200. |
| `Warning: MEZMO_LEDGER_INCIDENT_KEY=... is set ..., but not by ledger_investigation.py launch` | Say this loudly. The ledger hooks ignore an incident key in the environment unless `ledger_investigation.py launch` set it, so calls outside an investigation are not filed under it. A key left over from testing would otherwise file every call under that incident. Tell the user to remove it from where status says it's set, then restart Claude Code. To work on an incident, use `/mezmo:investigate`. |
| `Warning: MEZMO_LEDGER_ACCOUNT is set ...` | It's unused since 0.6.0: the ledger takes the account from the key. Tell the user to remove it. |

## Telemetry

1. Show the settings:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status
   ```

2. If telemetry is configured, test the endpoint and key. With `--check-only` in the arguments, nothing is published. Without it, one small test record per selected signal tagged `mezmo.test=true` is sent. By default, use the configured signals; `--signals` selects them explicitly and `--all-signals` deliberately tests all three. If configuration is ambiguous or disabled, resolve it or ask which signals to test; never silently broaden the selection.

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py" $ARGUMENTS
   ```

3. Explain the results using this table:

   | What you see | What to tell the user |
   | --- | --- |
   | Not configured | Run `/mezmo:setup telemetry`. |
   | Warnings or conflicting settings | Explain each one. Per-signal `OTEL_EXPORTER_OTLP_*_ENDPOINT`, `_PROTOCOL` or `_HEADERS` keys override the Mezmo settings. An `otelHeadersHelper` adds headers. `/mezmo:setup telemetry` can replace them. |
   | A note that a managed-settings file sets telemetry | Managed settings win over the user's file. Their administrator's settings decide where data goes. |
   | 401 or 403 | Mezmo rejected the ingestion key. Check it under **Settings > API Keys** in Mezmo, then rerun `/mezmo:setup telemetry`. |
   | 413 | A record is bigger than Mezmo's maximum message size. |
   | Network error | The machine can't reach `logs.mezmo.com` on port 443. Check the proxy, VPN or firewall. |
   | All checks pass, but no Claude Code data in Mezmo | See the list after this table. |

   If all checks pass but no Claude Code data shows up in Mezmo, these are the usual causes:

   - The session started before setup. Start a new session.
   - No Mezmo pipeline has an **OpenTelemetry** source yet. Add one.
   - Metrics are exported every 60 seconds, so wait a minute.
   - To see Claude Code's own export errors, the user can run this in a terminal and look for `[3P telemetry]` lines:

     ```bash
     claude -p "hello" --debug-file /tmp/claude-otel-debug.log && grep "3P telemetry" /tmp/claude-otel-debug.log
     ```

## MCP

1. Show whether the server is set up and connects:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_config.py" status
   ```

2. Explain the result:

   | What you see | What to tell the user |
   | --- | --- |
   | Not set up | The plugin brings the server once it has a service key: `/plugin configure mezmo@mezmo`, then `/reload-plugins`. |
   | `✔ Connected` | Claude Code connects. This session has tools named `mcp__plugin_mezmo_mezmo__<tool>` (or `mcp__mezmo__<tool>` from an earlier setup's server). |
   | Failed to connect, `401` | Mezmo rejected the service key, or none is set. Add a current key with `/plugin configure mezmo@mezmo`, then `/reload-plugins`. |
   | A server called `mezmo` with another URL | Something else uses the name `mezmo`. Remove it if it's in user scope: `claude mcp remove mezmo -s user`. |
   | Also has a `mezmo` server from an earlier setup | It duplicates the plugin's tools. Remove it with the command shown. |
   | `off (HTTP)`, URL unset or invalid | Mezmo MCP was turned off (`--no-mcp` saves the MCP URL as `off`). To turn it on, set the plugin's MCP URL with `/plugin configure mezmo@mezmo`, then `/reload-plugins`. |

3. If the server is set up, check whether this session has its tools: whether a tool named `mcp__plugin_mezmo_mezmo__get_ledger_investigation` (the plugin's server) or `mcp__mezmo__get_ledger_investigation` (an earlier setup's) is among your tools, loaded or deferred. Don't call it. If neither is available, tell the user: "Run /reload-plugins (or start a new Claude Code session) to use the Mezmo MCP."

## Verify arrival and an actual governed call

Report endpoint acceptance as one step, not proof of destination arrival. Have the user
find the printed `test.run_id` with `service.name=mezmo-claude-plugin` and `mezmo.test=true`
in their pipeline destination. Then, in a new Claude session, ask for the harmless Bash
command `printf 'mezmo-onboarding-test\n'` and confirm its decision under the intended
Governance agent. Read/Edit/Write are intentionally not governed. Governance sends tool
arguments even with telemetry content capture off. Actual Claude spans use
`service.name=claude-code`; verify those separately. Mark each step not yet observed as
unverified. Do not create tokens, change policies or pipelines, or select accounts without
authorization.
