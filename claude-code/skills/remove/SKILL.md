---
name: remove
description: Undo the Mezmo setup for Claude Code - turn off the Mezmo Governance tool-call check, stop sending telemetry to Mezmo, remove the Mezmo MCP server, or all of them.
argument-hint: "[governance|telemetry|mcp|all]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_config.py" *)
---

# Remove the Mezmo setup

Arguments: $ARGUMENTS

Unless the arguments already say, ask what to remove: governance, telemetry, MCP, or all of them.

## Governance

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" remove
```

This clears the plugin's governance options, overwriting the saved key (a sensitive plugin option, in the macOS Keychain or `~/.claude/.credentials.json`) and turning governance and the ledger off, and removes any `MEZMO_GOVERNANCE_*`, `MEZMO_LEDGER_URL` and `MEZMO_LEDGER_KEY` variables from the user settings file. If the script reports that managed settings or the environment still set governance, only whoever set those can remove them.

## Telemetry

1. Run:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" remove
   ```

2. Read the exit code:

   - **0**: tell the user which keys were removed and where the backup is. Settings for other exporters, such as `console`, and `OTEL_RESOURCE_ATTRIBUTES` stay in place.
   - **4**: the settings don't point at Mezmo, so nothing was removed. Show the user what `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status` reports. Run again with `--force` only if they ask for every OTLP setting to be removed.
   - **1**: show the error.

## MCP

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" remove --mcp-only
```

This turns off the plugin's `mezmo` MCP server by saving its MCP URL as `off`, so it connects
nowhere, and removes a user-scope `mezmo` server an earlier setup added. Governance and the ledger
keep their key. Removing governance (above) turns the MCP server off too, since it shares the key.
A `mezmo` server from a project's `.mcp.json` or local config isn't the plugin's: the user removes
it there.

## Tell the user

- Run `/reload-plugins` to apply plugin option changes now, or start a new Claude Code session; telemetry changes apply in a new session.
- Uninstalling the plugin removes its own MCP server, but not the telemetry settings or a `mezmo` server an earlier setup added. That's why this command exists.
- Settings deployed through managed settings have to be removed by the administrator.

If governance removal reports incomplete cleanup, explain which settings were removed and that the plugin token may remain. Do not claim governance is fully removed. Retry once Claude Code can read and write its plugin options again.
