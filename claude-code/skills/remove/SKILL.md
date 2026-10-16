---
name: remove
description: Undo the Mezmo setup for Claude Code - turn off the Mezmo Governance tool-call check, stop sending telemetry to Mezmo, or both.
argument-hint: "[governance|telemetry|both]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" *)
---

# Remove the Mezmo setup

Arguments: $ARGUMENTS

Unless the arguments already say, ask whether to remove governance, telemetry, or both.

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

## Tell the user

- Start a new Claude Code session for the change to take effect.
- Uninstalling the plugin doesn't remove the telemetry settings. That's why this command exists.
- Settings deployed through managed settings have to be removed by the administrator.
