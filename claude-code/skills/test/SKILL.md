---
name: test
description: Check the Mezmo setup for Claude Code. Shows the Mezmo Governance configuration with the governance hook's last result, and the telemetry settings with a test of the ingestion key against Mezmo's OpenTelemetry endpoint. Use when the user asks why a tool call was blocked by Mezmo Governance, whether Mezmo governance or telemetry works, or why Claude data is missing in Mezmo.
argument-hint: "[--check-only]"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" status*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" check*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py" *)
---

# Test the Mezmo setup

Arguments: $ARGUMENTS

## Governance

1. Show the configuration, the latest session's counts and the hook's last result:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" status
   ```

2. If the access key comes from settings or the environment, check it. This asks the service about a random decision and records nothing:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" check
   ```

   A key kept in the system keychain can't be read back. In that case the last result shows whether the hook is working, or the user can pass `--key` to check a key directly.

3. Explain the results using this table:

   | What you see | What to tell the user |
   | --- | --- |
   | Governance is off | Tool calls run without a check. Run `/mezmo:setup governance` to turn it on. |
   | Required but not configured | Every tool call is blocked until governance is configured. Ask their administrator for the agent access key. |
   | `HTTP 401` | Mezmo Governance rejected the access key. Rerun `/mezmo:setup governance` with a current key. |
   | `ENOAGENT`, `EAGENTINACTIVE` or `EPOLICYUNASSIGNED` | The key is valid, but no active agent with a policy is registered for it. An administrator fixes this in Mezmo Governance. |
   | `approval still pending` | The policy sent the call to a person, and nobody decided in time. The decision is in Mezmo Governance. Approving it there lets the same call through when Claude retries it. |
   | `argument name '-i' isn't accepted` | The service only accepts plain identifiers as argument names. Calls with such names are blocked until the service accepts them. |
   | `Mezmo Governance is unavailable` | The machine can't reach the authorize URL. Check the URL, proxy, VPN or firewall. |

## Telemetry

1. Show the settings:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status
   ```

2. If telemetry is configured, test the endpoint and key. With `--check-only` in the arguments, nothing is published. Without it, three small test records tagged `mezmo.test=true` are sent.

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
