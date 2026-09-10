# Mezmo Governance plugins

Plugins that ask Mezmo Governance before a coding agent runs a tool. If Mezmo
denies the call, or never answers, the tool does not run.

Copy the directory for the agent you use.

## How it works

```text
Codex / Claude Code / Cursor / Pi / OpenCode / LangChain / Mastra
        │  before the tool runs
        ▼
  plugin  ──POST /governance/workflow/authorize──►  Mezmo
        │
        │  { "approved": true|false, "reason"? }
        ▼
  run the tool, or stop it and show the reason
```

Some plugins append a line to a local JSONL file after a tool finishes.

Request shape, polling, and identity rules are in
[docs/protocol.md](docs/protocol.md).

## Supported agents

| Directory | Mechanism | Stops the tool | After the tool | Install |
| --- | --- | --- | --- | --- |
| [codex](codex/) | `PreToolUse` / `PostToolUse` command hooks | yes | local JSONL | `~/.codex/hooks.json` or `.codex/hooks.json` |
| [claude-code](claude-code/) | same hook events, or a Claude Code plugin | yes | local JSONL | `~/.claude/settings.json`, or this directory as a plugin |
| [cursor](cursor/) | `preToolUse` (optional MCP/shell hooks) | yes (`failClosed: true`) | local JSONL | `~/.cursor/hooks.json` or `.cursor/hooks.json` |
| [pi](pi/) | `tool_call` / `tool_result` extension | yes | local JSONL | `~/.pi/agent/extensions/` or `.pi/extensions/` |
| [opencode](opencode/) | `tool.execute.before` / `tool.execute.after` plugin | yes (throw to block) | local JSONL | `.opencode/plugins/` |
| [langchain](langchain/) | `AgentMiddleware.awrap_tool_call` | yes | optional OpenTelemetry span | `middleware=[MezmoGovernanceMiddleware(...)]` |
| [mastra](mastra/) | `hooks.beforeToolCall` / `afterToolCall` | yes (`proceed: false`) | local JSONL | `hooks: mezmoGovernanceHooks` |

## Setup

In Mezmo, create a policy and register an agent for the coding tool you are
installing. Copy the access key. Mezmo shows it once.

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

For development, use
`https://api.use.dev.mezmo.it/governance/workflow/authorize` instead.

Register a separate agent for each coding tool so policy and logs stay
separate. Then follow the README in that tool's directory.

## Environment

| Variable | Required | Default |
| --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | — |
| `MEZMO_GOVERNANCE_ACCESS_KEY` | yes | — |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590` |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` |

Hook JSON `timeout` values in the examples are 600 seconds so the plugin's
590 second deadline finishes first.

## Tests

Python 3.10+ and Node 22+ (type stripping for the TypeScript tests):

```bash
make test
```

Each directory's tests mock HTTP. They do not call Mezmo.

## Layout

```text
codex/          Codex CLI command hook
claude-code/    Claude Code hook + plugin manifest
cursor/         Cursor agent hooks.json adapter
pi/             Pi coding-agent extension
opencode/       OpenCode plugin
langchain/      LangChain AgentMiddleware
mastra/         Mastra agent hooks
docs/protocol.md
```

Apache License 2.0. See [LICENSE](LICENSE).
