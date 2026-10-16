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

This clears the plugin's governance options, including the key in the system keychain, and removes any `MEZMO_GOVERNANCE_*` variables from the user settings file. If the script reports that managed settings or the environment still set governance, only whoever set those can remove them.

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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mcp_config.py" remove
```

This removes the user-scope `mezmo` MCP server, and its service key with it. Exit code **4** means the server comes from a project's `.mcp.json` or local config, so the user has to remove it there.

## Tell the user

- Start a new Claude Code session for the change to take effect.
- Uninstalling the plugin doesn't remove the telemetry settings or the MCP server. That's why this command exists.
- Settings deployed through managed settings have to be removed by the administrator.
