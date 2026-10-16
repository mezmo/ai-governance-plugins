---
name: setup
description: Set up Mezmo for Claude Code - check every tool call with Mezmo Governance, send telemetry (metrics, events and traces) to Mezmo, or both - for this user or as a managed-settings file for an organization.
argument-hint: "[governance|telemetry|both]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" *)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py" *)
---

# Set up Mezmo for Claude Code

The plugin does two independent things:

- **Governance**: before each tool call, a hook asks Mezmo Governance whether this exact call may run and blocks it when the answer is no. It uses an agent access key. Until one is configured, the hook does nothing.
- **Telemetry**: Claude Code exports OpenTelemetry metrics, events and traces to Mezmo's endpoint, `https://logs.mezmo.com/otel`, authenticated with an ingestion key in an `apikey` header.

Both take effect in new Claude Code sessions.

Arguments: $ARGUMENTS

## 1. Look at the current setup

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" status
```

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" status
```

If `python3` isn't found, tell the user the plugin needs Python 3.8 or later, and stop.

## 2. Ask what to set up

Unless the arguments already say, ask in one AskUserQuestion call:

1. **What to set up**: Governance (default), Telemetry, or Both.
2. **Where**:
   - Just me: this user's Claude Code settings
   - My organization: a managed-settings file for an administrator to deploy

Never repeat a full key back to the user. Refer to keys by their first and last four characters, as the scripts do.

## 3. Governance

Skip this section if the user didn't choose governance.

### Access key and environment

Ask for the **agent access key**. In Mezmo, an administrator registers an agent for Claude Code, attaches a policy and copies the agent's access key, which Mezmo shows once. Register a separate agent for each coding tool, so policies and decisions stay separate. The token AURA reads from `MEZMO_GOVERNANCE_TOKEN` is the same kind of key.

Ask which environment:

- Production (default): `https://api.mezmo.com/governance/workflow/authorize`
- Development: `https://api.use.dev.mezmo.it/governance/workflow/authorize`
- Another authorize URL

Before saving, tell the user what changes:

- Every tool call waits for Mezmo Governance before it runs.
- A call the policy denies is blocked with the policy's reason.
- A call the policy sends to a person waits for their decision, up to about 10 minutes, and is blocked if nobody decides.
- If Mezmo Governance can't be reached, calls are blocked.
- Mezmo Governance rejects argument names that aren't plain identifiers, such as Grep's `-i` or `-C`, so those calls are blocked until the service accepts them.

### Just me

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" configure --key <KEY> --url <URL>
```

Leave out `--url` for production. The script checks the key first, without recording anything, then saves it as a plugin option, which keeps the key in the system keychain.

What each exit code means:

- **0**: done. Report what the script printed.
- **3**: Mezmo Governance rejected the key, or couldn't be reached. Show the message. Nothing was saved.
- **4**: the plugin isn't installed from a marketplace (for example, it was loaded with `--plugin-dir`), so it has no options to save. Ask whether to keep the key in the settings file's `env` instead, which stores it as plain text. If yes, run the same command with `--store settings`.
- **2** or **1**: show the message.

### My organization

Ask whether governance should be **required**. With `--required`, Claude Code blocks every tool call on a machine where governance isn't configured, instead of letting it run.

Write the managed-settings document to a file, not to the transcript:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" managed --key <KEY> --url <URL> [--required] --output ./mezmo-managed-settings.json
```

Check the key with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/governance_config.py" check --key <KEY> --url <URL>`.

For governance to be enforced, the plugin itself also has to be installed and enabled on each machine. An administrator can add the marketplace with `extraKnownMarketplaces` and turn the plugin on with `enabledPlugins` (`"mezmo@mezmo": true`) in the same managed settings.

## 4. Telemetry

Skip this section if the user didn't choose telemetry.

### Ingestion key

- If the telemetry status shows Mezmo is already configured, offer to keep the current key. To keep it, leave `--key` out of the `configure` command.
- Otherwise, ask the user for their Mezmo ingestion key. It's under **Settings > API Keys** in Mezmo.

### What to send

Ask in one AskUserQuestion call:

1. **Signals** (multi-select, all three by default):
   - Metrics: cost, tokens, sessions, lines of code, commits, active time
   - Events: prompts, API requests and errors, tool results and decisions, sent as OTel logs
   - Traces (beta): a span for each prompt, model request and tool call
2. **Content** (multi-select). All of these are off by default. Tell the user that this text leaves the machine and is stored in Mezmo.
   - Prompt text
   - Response text
   - Tool details: commands, file paths and tool inputs
   - None

Only if the user mentions teams, departments or cost tracking, ask for resource attributes such as `team.id=platform,cost_center=eng-123`.

Build the flags: `--no-metrics`, `--no-logs` or `--no-traces` for each signal left out; `--log-prompts`, `--log-responses` or `--log-tool-details` for each kind of content chosen; and `--resource-attributes <list>` if given.

### Just me

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" configure --key <KEY> <flags>
```

What each exit code means:

- **0**: done. Report what the script printed.
- **2**: bad input, such as a malformed key or resource attribute. Show the message and ask again.
- **3**: the settings already send OpenTelemetry somewhere else, or have settings that would override Mezmo. Show the user the keys the script lists. Explain that Claude Code exports to one OTLP endpoint. Ask whether to replace them. If the user says yes, run the same command with `--force`. The script backs up the file first.
- **1**: the settings file couldn't be read or written. Show the message.

Then check the connection. The script reads the key from the settings:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py"
```

The check sends Mezmo's connection check for traces, logs and metrics, then three small test records tagged `mezmo.test=true`. Report the results.

### My organization

Add telemetry to the same managed-settings file as governance. The script replaces only its own keys:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/claude_code_otel.py" managed --key <KEY> <flags> --output ./mezmo-managed-settings.json
```

Check the key with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mezmo_check.py" --key <KEY>`.

## 5. Deploying the managed-settings file

For "My organization", tell the administrator:

- The file contains the keys, so handle it like a secret and delete the local copy once it's in place.
- Deploy it in one of these ways:
  - As a drop-in file in the managed settings directory (`managed-settings.d/` is next to `managed-settings.json`):
    - macOS: `/Library/Application Support/ClaudeCode/managed-settings.d/mezmo.json`
    - Linux and WSL: `/etc/claude-code/managed-settings.d/mezmo.json`
    - Windows: `C:\Program Files\ClaudeCode\managed-settings.d\mezmo.json`
  - Paste its `env` block into server-managed settings in the claude.ai admin console. This is the only route that reaches cloud sessions.
  - Deliver it through MDM.
- Managed settings override each developer's own settings.

Don't run `sudo` or copy the file into a system directory yourself.

## 6. Wrap up

Tell the user:

- Start a new Claude Code session. The current one keeps its old settings.
- Governance: decisions show up in Mezmo Governance under the agent for this access key. While a call waits for a person, a band above the prompt shows it, and the footer counts this session's checks. After each approved call, the hook appends a line to `~/.mezmo/governance-audit.jsonl`.
- Telemetry: Mezmo routes the data once a pipeline has an **OpenTelemetry** source. In Mezmo, look for `service.name` = `claude-code`: metrics such as `claude_code.cost.usage` and `claude_code.token.usage`, events such as `claude_code.api_request` and `claude_code.tool_result`, and spans such as `claude_code.interaction`.
- `/mezmo:test` checks the setup again, and `/mezmo:remove` undoes it.
