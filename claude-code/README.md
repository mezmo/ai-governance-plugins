# Mezmo plugin for Claude Code

This plugin puts Claude Code's tool calls under [Mezmo](https://www.mezmo.com) Governance. Before
Claude runs a tool, the plugin asks Mezmo Governance whether that exact call is allowed, and
blocks it if the policy says no.

It can also send Claude Code's metrics, events and traces to Mezmo's
[OpenTelemetry source](https://docs.mezmo.com/telemetry-pipelines/open-telemetry-source), and
connect Claude to [Mezmo MCP](https://docs.mezmo.com/docs/mezmo-mcp) so it can query your logs and
work on pipelines. All three are independent, so you can turn on any of them.

A small status display in the prompt footer tells you whether governance and telemetry are
working.

## Install

You need Python 3.9 or later. The scripts only use the standard library.
You need Claude Code v2.1.295 or later: older versions ignore the hook's `onFailure: "block"`,
so a governance hook that crashes or times out lets the tool run.

Tested version: Claude Code 2.1.301 with this plugin, run live against
`tools/mock_governance.py`: `Bash` was denied and `Read` was approved, and a
hook crash blocked the call.

```bash
claude plugin marketplace add mezmo/ai-governance-plugins
```

```bash
claude plugin install mezmo@mezmo
```

Or, from a clone of this repository, run `./mezmo-governance install claude-code`
(add `--dry-run` to see the change first). It removes the older
`mezmo-governance` plugin, which would check every call twice. It then
installs `mezmo` from the clone, unless a `mezmo` marketplace is already set
up. Keep the clone where it is: Claude Code loads the plugin from it.
`./mezmo-governance uninstall claude-code` undoes it. Moving between this
route and the GitHub marketplace deletes the saved key (see below). After you
update the clone, run `uninstall`, then `install`: Claude Code keeps its own
copy, and `install` leaves an existing one alone.

Then run `/mezmo:setup` in Claude Code and start a new session. Claude Code reads these settings
when a session starts, so the session you ran setup in carries on as before.

Removing the marketplace, or uninstalling the plugin, deletes its saved settings, including the
governance service account token in the keychain. That includes moving the plugin to another marketplace.
Afterwards, run `/reload-plugins` or start a new session, then run `/mezmo:setup governance`
again. Telemetry settings live in `~/.claude/settings.json` and the MCP server in
`~/.claude.json`, so they aren't affected.

| Command | What it does |
| --- | --- |
| `/mezmo:setup [governance\|telemetry\|mcp\|all]` | Sets up governance, telemetry, MCP or all of them. Governance and telemetry can also go in a managed-settings file for your organization |
| `/mezmo:test` | Checks your keys and shows what's configured |
| `/mezmo:remove [governance\|telemetry\|mcp\|all]` | Undoes setup. Uninstalling the plugin doesn't |
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

Governance stays off until you give it a key. Until then the hook lets every call through and
sends nothing. Register an agent for Claude Code in Mezmo Governance, give it a service account
with Governance permissions, and attach a policy. Then run `/mezmo:setup governance` with that
service account's token, a standard Mezmo service key (see
[Connect to Mezmo](../README.md#connect-to-mezmo)). Setup checks the key without recording a
decision, then saves it as a plugin option, so it lives in the system keychain rather than in a
settings file.

| Setting | Plugin option | Environment variable (wins) | Default |
| --- | --- | --- | --- |
| Authorize URL | `governance_url` | `MEZMO_GOVERNANCE_URL` | `https://api.mezmo.com/governance/workflow/authorize` |
| Service account token | `governance_access_key` | `MEZMO_GOVERNANCE_ACCESS_KEY` or `MEZMO_GOVERNANCE_TOKEN` | none, so governance is off |
| Required | | `MEZMO_GOVERNANCE_REQUIRED=1` | off |

The service only accepts plain identifiers as argument names, so a Grep call with `-i` gets
blocked, and the message says which argument. Also, `--safe-mode` and `"disableAllHooks": true`
turn the governance check off along with your other hooks.

To check the setup from the repository root, run `./mezmo-governance doctor claude-code` and
`./mezmo-governance smoke claude-code`. `doctor` reads `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_TOKEN` from the shell, not the key that setup saved, so export them first.
`smoke` uses its own mock.
`/mezmo:test` checks the saved key from inside Claude Code.

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

## Mezmo MCP

[Mezmo MCP](https://docs.mezmo.com/docs/mezmo-mcp) gives Claude tools to analyze your logs (root
causes, patterns, histograms, correlated timelines), look at traces and AI investigations, and
inspect and change Telemetry Pipelines. You need a Mezmo service key, from Settings > API Keys in
Mezmo. It's a different key from the ingestion key.

`/mezmo:setup mcp` checks the key against `https://mcp.mezmo.com/mcp`, then runs the command
Mezmo documents:

```bash
claude mcp add --scope user --transport http mezmo https://mcp.mezmo.com/mcp --header "Authorization: Bearer <service key>"
```

The server is available in every project, in new sessions, with tools named `mcp__mezmo__<tool>`.
Claude Code keeps the key as plain text in `~/.claude.json`, as with any server added this way.
Some tools change pipelines: they create, update, delete, pause and publish them. Claude Code asks
before each MCP tool call unless you allow it. With governance on, these calls also go through
Mezmo Governance under namespace `mezmo`, so a policy can send deletes and publishes to a person.

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
| Tool calls blocked with `HTTP 401` | Mezmo Governance rejected the service account token. Run `/mezmo:setup governance` again |
| Blocked with `ENOAGENT`, `EAGENTINACTIVE` or `EPOLICYUNASSIGNED` | The key works, but no active agent with a policy is linked to it |
| Blocked with `approval still pending` | Nobody decided in time. Approve the call in Mezmo, and Claude's retry goes through |
| Every call blocked, and governance isn't set up | Your organization set `MEZMO_GOVERNANCE_REQUIRED=1`. Ask them for the service account token |
| Every call blocked with `HTTP 401` right after reinstalling the plugin | The running session still has the old plugin's settings. Run `/reload-plugins` or start a new session, then `/mezmo:setup governance` |
| Governance is off after reinstalling the plugin | Reinstalling deleted the saved service account token. Run `/mezmo:setup governance` again |
| Telemetry checks pass, but nothing shows up in Mezmo | Start a new session, make sure a pipeline has an OpenTelemetry source, and give metrics a minute |
| Telemetry setup reports conflicting settings | Your settings already send OpenTelemetry somewhere else. Claude Code only exports to one endpoint |
| The `mezmo` MCP server fails to connect with `401` | Mezmo rejected the service key. Run `/mezmo:setup mcp` with a current key and replace the server |

To see Claude Code's own telemetry export log:

```bash
claude -p "hello" --debug-file /tmp/claude-otel.log && grep "3P telemetry" /tmp/claude-otel.log
```

## Development

```bash
python3 -m unittest discover -s tests
```

From the repo root: `make test-claude-code`. `make test-claude-code-mod` runs `claude plugin test`.

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

`scripts/` holds the Python that runs the governance hook, writes settings, adds the MCP server and
checks keys.
`skills/` has the slash commands, and `hooks/` has the mod and the governance hook registration.
