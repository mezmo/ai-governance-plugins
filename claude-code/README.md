# Mezmo plugin for Claude Code

This plugin puts Claude Code's tool calls under [Mezmo](https://www.mezmo.com) Governance. Before
Claude runs a tool, the plugin asks Mezmo Governance whether that exact call is allowed, and
blocks it if the policy says no.

It can also send Claude Code's metrics, events and traces to Mezmo's
[OpenTelemetry source](https://docs.mezmo.com/telemetry-pipelines/open-telemetry-source). The two
are independent, so you can turn on either one.

A small status display in the prompt footer tells you whether each one is working.

## Install

You need Python 3.8 or later. The scripts only use the standard library.

```bash
claude plugin marketplace add mezmo/ai-governance-plugins
```

```bash
claude plugin install mezmo@mezmo
```

Then run `/mezmo:setup` in Claude Code and start a new session. Claude Code reads these settings
when a session starts, so the session you ran setup in carries on as before.

Removing the marketplace, or uninstalling the plugin, deletes its saved settings, including the
governance access key in the keychain. That includes moving the plugin to another marketplace.
Afterwards, run `/reload-plugins` or start a new session, then run `/mezmo:setup governance`
again. Telemetry settings live in `~/.claude/settings.json` and aren't affected.

| Command | What it does |
| --- | --- |
| `/mezmo:setup [governance\|telemetry\|both]` | Sets up governance, telemetry or both, for you or as a managed-settings file for your organization |
| `/mezmo:test` | Checks your keys and shows what's configured |
| `/mezmo:remove [governance\|telemetry\|both]` | Undoes setup. Uninstalling the plugin doesn't |
| `/mezmo` | Shows this session's status right away, without a Claude turn |

To try the plugin for one session without installing it, run
`claude --plugin-dir /path/to/ai-governance-plugins/claude-code`.

## Governance

With governance on, a `PreToolUse` hook sends each tool call to Mezmo Governance before it runs:

| Policy says | What happens |
| --- | --- |
| Allow | The tool runs |
| Deny | The tool is blocked, and Claude sees the policy's reason |
| Ask a person | The call waits while someone approves or denies it in Mezmo, for up to 590 seconds. If nobody decides, it's blocked |
| Nothing: the service is unreachable, or rejects the key | The tool is blocked |

Governance stays off until you give it an access key. Until then the hook lets every call through
and sends nothing. Register an agent for Claude Code in Mezmo Governance, attach a policy, and run
`/mezmo:setup governance` with the agent's access key. Setup checks the key without recording a
decision, then saves it as a plugin option, so it lives in the system keychain rather than in a
settings file.

| Setting | Plugin option | Environment variable (wins) | Default |
| --- | --- | --- | --- |
| Authorize URL | `governance_url` | `MEZMO_GOVERNANCE_URL` | `https://api.mezmo.com/governance/workflow/authorize` |
| Access key | `governance_access_key` | `MEZMO_GOVERNANCE_ACCESS_KEY` or `MEZMO_GOVERNANCE_TOKEN` | none, so governance is off |
| Required | | `MEZMO_GOVERNANCE_REQUIRED=1` | off |

Development uses `https://api.use.dev.mezmo.it/governance/workflow/authorize`.

The service only accepts plain identifiers as argument names, so a Grep call with `-i` gets
blocked, and the message says which argument. Also, `--safe-mode` and `"disableAllHooks": true`
turn the governance check off along with your other hooks.

## Telemetry

You need a Mezmo ingestion key, from Settings > API Keys in Mezmo, and a pipeline with an
OpenTelemetry source. Mezmo accepts data without that source but doesn't route it anywhere.

A plugin can't set environment variables for Claude Code, so `/mezmo:setup telemetry` writes them
into the `env` block of `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`). It backs
the file up first, and stops to ask if the file already sends OpenTelemetry somewhere else. With
every signal on, it writes:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_TRACES_EXPORTER": "otlp",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://logs.mezmo.com/otel",
    "OTEL_EXPORTER_OTLP_HEADERS": "apikey=<your Mezmo ingestion key>"
  }
}
```

Prompt text, response text and tool details stay out unless you opt in during setup. You can also
copy the block above in by hand. It has to go in your user settings or managed settings, because
Claude Code ignores these variables in a repository's `.claude/settings.json`.

In Mezmo, filter on `service.name` = `claude-code`. Cost and tokens are in the
`claude_code.cost.usage` and `claude_code.token.usage` metrics and in `claude_code.api_request`
events. [Claude Code monitoring](https://code.claude.com/docs/en/monitoring-usage) lists everything
else Claude Code sends.

## Status in Claude Code

The plugin includes a [mod](https://code.claude.com/docs/en/plugins/mods/overview) that adds one
label at the right of the prompt footer, such as `Mezmo · Governance 14 ✓ 1 ✗ · Telemetry ✓`.
It only shows the parts you've set up:

| Part | Meaning |
| --- | --- |
| `Governance 14 ✓ 1 ✗` | This session's governance checks so far |
| `Governance ⏳ 1 waiting` | A tool call is waiting for a person to decide |
| `Telemetry ✓` | This session sends telemetry to Mezmo |
| `Telemetry: start a new session to begin exporting` | You set up telemetry after this session started |
| `Telemetry ✗ key rejected`, `Telemetry ✗ unreachable` | The telemetry key check it runs once per session failed |

While a call waits for a person, a band above the prompt shows which call it is, how long it has
waited and the policy's reason, and the time ticks up every second. When the person decides, a
toast tells you. `/mezmo` prints the same information as text, and also works in `claude -p`.

The mod needs Claude Code 2.1.287 or later. Governance doesn't depend on it: the Python hook does
the blocking, so it keeps working where an organization blocks user-installed mods. The hook keeps
each session's counts and pending decisions in `~/.mezmo/governance/sessions/` for the mod to
read. It never writes tool arguments there.

## Rolling out to an organization

Pick "My organization" in `/mezmo:setup`. It writes one managed-settings file with the governance
and telemetry variables, plus `MEZMO_GOVERNANCE_REQUIRED=1` if you want every tool call blocked
on machines where governance isn't set up. The file holds your keys, so treat it like a secret.
Deploy it one of three ways:

- As a drop-in in `managed-settings.d/`, under `/Library/Application Support/ClaudeCode/` on
  macOS, `/etc/claude-code/` on Linux and WSL, or `C:\Program Files\ClaudeCode\` on Windows.
- In [server-managed settings](https://code.claude.com/docs/en/server-managed-settings) in the
  claude.ai admin console. This is the only route that reaches cloud sessions.
- Through MDM.

Governance also needs the plugin installed on each machine. Add `extraKnownMarketplaces` and
`enabledPlugins` (`"mezmo@mezmo": true`) to the same file to handle that.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Tool calls blocked with `HTTP 401` | Mezmo Governance rejected the access key. Run `/mezmo:setup governance` again |
| Blocked with `ENOAGENT`, `EAGENTINACTIVE` or `EPOLICYUNASSIGNED` | The key works, but no active agent with a policy is linked to it |
| Blocked with `approval still pending` | Nobody decided in time. Approve the call in Mezmo, and Claude's retry goes through |
| Every call blocked, and governance isn't set up | Your organization set `MEZMO_GOVERNANCE_REQUIRED=1`. Ask them for the access key |
| Every call blocked with `HTTP 401` right after reinstalling the plugin | The running session still has the old plugin's settings. Run `/reload-plugins` or start a new session, then `/mezmo:setup governance` |
| Governance is off after reinstalling the plugin | Reinstalling deleted the saved access key. Run `/mezmo:setup governance` again |
| Telemetry checks pass, but nothing shows up in Mezmo | Start a new session, make sure a pipeline has an OpenTelemetry source, and give metrics a minute |
| Telemetry setup reports conflicting settings | Your settings already send OpenTelemetry somewhere else. Claude Code only exports to one endpoint |

To see Claude Code's own telemetry export log:

```bash
claude -p "hello" --debug-file /tmp/claude-otel.log && grep "3P telemetry" /tmp/claude-otel.log
```

## Development

```bash
python3 -m unittest discover -s tests
```

```bash
claude plugin test
```

```bash
claude plugin validate .claude-plugin/plugin.json
```

Validation also lists every event the mod hooks and every call it makes. To type-check the mod,
use the config Claude Code generates when it validates or tests the plugin:

```bash
npx -p typescript tsc -p .claude-plugin/types/tsconfig.json
```

`scripts/` holds the Python that runs the governance hook, writes settings and checks keys.
`skills/` has the slash commands, and `hooks/` has the mod and the governance hook registration.
