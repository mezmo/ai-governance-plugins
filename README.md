# Mezmo Governance plugins

Plugins that ask Mezmo Governance before a coding agent runs a tool. If Mezmo
denies the call, or never answers, the tool does not run.

```text
Claude Code / Codex / Cursor / Gemini CLI / Copilot / Pi / OpenCode / LangChain / Mastra
        │  before the tool runs
        ▼
  plugin  ──POST /governance/workflow/authorize──►  Mezmo Governance
        │
        │  { "approved": true|false, "reason"? }
        ▼
  run the tool, or stop it and show the reason to the agent
```

## Try it in two minutes, without a Mezmo account

A local mock server stands in for Mezmo, so you can watch a plugin block a
call before you connect it to anything. You need Python 3.9+ and Claude Code
2.1.295+.

1. Start the mock and deny `Bash`:

   ```bash
   git clone https://github.com/mezmo/ai-governance-plugins
   cd ai-governance-plugins
   python3 tools/mock_governance.py --deny Bash
   ```

2. In a second terminal, start Claude Code with the plugin loaded:

   ```bash
   cd ai-governance-plugins
   export MEZMO_GOVERNANCE_URL=http://127.0.0.1:8787/governance/workflow/authorize
   export MEZMO_GOVERNANCE_ACCESS_KEY=local-test
   claude --plugin-dir ./claude-code
   ```

3. Ask Claude to run `ls`. The mock prints `denied claude_code:Bash`, and
   Claude reports `Mezmo Governance denied this exact tool call: mock policy:
   claude_code:Bash is denied`. Ask it to read a file: that call is approved.

Run `python3 tools/mock_governance.py --help` for patterns (`--deny 'mcp__*'`,
`--deny 'kubernetes:pods_*'`) and `--hold`, which leaves a call pending so you
can watch the plugin wait and then deny.

## Connect to Mezmo

1. In Mezmo Governance, create a policy and register one agent per coding
   tool, so each has its own policy and log. Copy the agent's access key:
   Mezmo shows it once.
2. Install the plugin for your agent (table below).
3. Give it the URL and key. Claude Code prompts for both when you enable the
   plugin. OpenCode 2.x takes them as plugin options in `opencode.json`
   (see [opencode/](opencode/)), because its tools run in a background
   service that does not see your shell. Every other agent reads environment
   variables:

   ```bash
   export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
   export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
   ```

4. Run one tool call your policy denies, and check that the agent shows the
   reason. Then check `~/.mezmo/governance-audit.jsonl` for the approved calls.

### Claude Code

```text
/plugin marketplace add mezmo/ai-governance-plugins
/plugin install mezmo-governance@mezmo
```

Claude Code asks for the authorize URL and the access key and keeps the key in
the system credential store. Details: [claude-code/](claude-code/).

### Other agents

| Agent | Hook | Fails closed? | Install |
| --- | --- | --- | --- |
| [Codex CLI](codex/) | `PreToolUse` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `hooks.example.json`, trust it in `/hooks` |
| [Cursor](cursor/) | `preToolUse` hook | yes, with `failClosed: true` | copy the script, merge `hooks.example.json` |
| [Gemini CLI](gemini-cli/) | `BeforeTool` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `settings.example.json` |
| [GitHub Copilot](copilot/) | `preToolUse` hook (CLI, cloud agent, VS Code) | on a crash yes; on a timeout no | copy the script, add `hooks.example.json` to `~/.copilot/hooks/` |
| [Pi](pi/) | `tool_call` extension | yes | copy the folder to `~/.pi/agent/extensions/` |
| [OpenCode](opencode/) 1.x and 2.x | `tool.execute.before` plugin | yes | 2.x: copy the folder, name it in `opencode.json` `plugins` with options; 1.x: copy to `~/.config/opencode/plugins/` |
| [LangChain](langchain/) v1 | `AgentMiddleware` (`wrap_tool_call` and `awrap_tool_call`) | yes | `pip install ./langchain` |
| [Mastra](mastra/) | agent `hooks.beforeToolCall` | yes | copy the files, pass `hooks` |

"Fails closed" means the tool does not run when the plugin itself crashes,
times out, or cannot start. Every plugin denies the call when Mezmo denies it,
cannot be reached, or returns something it does not understand.

## What leaves your machine

Before each tool call, the plugin sends Mezmo the tool name and its full
arguments: the shell command, the file path, and for write tools the file
content. It also sends the session id and the tool call id (Gemini CLI and Copilot send a fresh random id per call, because they expose none). Tool results are
not sent: post-tool hooks only append a line to a local file.

Write policy on the assumption that Mezmo sees every argument, and keep secrets
out of tool arguments.

## Settings

| Variable | Required | Default |
| --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | none |
| `MEZMO_GOVERNANCE_ACCESS_KEY` | yes | none |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590`, the longest the plugin waits for a person to decide |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` |

The hook examples set a 600 second hook timeout, so the plugin's own deadline
ends first and returns a clear deny.

## Troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| Every call runs and the mock prints nothing | the hook never ran | check the path in the hook config; Codex: trust the hook in `/hooks`; Claude Code: look for a `hook error` notice; Copilot: install in `~/.copilot/hooks/`, not `.github/hooks/`; Gemini CLI: use `~/.gemini/settings.json` or trust the folder |
| Every call is denied: `MEZMO_GOVERNANCE_URL must be a non-empty string` | the agent did not inherit the variables | export them in the shell that starts the agent, or use the agent's own settings `env` block |
| `Mezmo Governance returned HTTP 401` | wrong or revoked access key | copy a fresh key from the agent in Mezmo |
| `approval still pending` | policy needs a person and nobody decided in time | decide in Mezmo, or raise `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` (max 590) |
| `SyntaxError` or `TypeError` on start | `python3` is older than 3.9 | install a newer Python, or point the hook command at it |

The protocol, including polling and request identity, is in
[docs/protocol.md](docs/protocol.md).

## Tests

Python 3.9+ and Node 22+. Tests mock HTTP and never call Mezmo.

```bash
make test
```

`make test` runs the LangChain middleware tests only when LangChain is
installed in `langchain/.venv` (see [langchain/](langchain/)).

### What the tests confirm

Each adapter's tests feed it the payload its runtime sends and run the real
entry point: the hook script as a subprocess, or the plugin's exported hooks.
A local HTTP server stands in for Mezmo. The tests confirm that:

- an approved call runs unchanged, and a denied call is blocked with Mezmo's
  reason in the runtime's own deny format;
- the call is blocked when the URL or key is missing, Mezmo cannot be
  reached, a decision stays pending, or an answer arrives (or drips in)
  after the deadline; the Python hooks also block on malformed input from
  the runtime;
- a `409` falls into polling, and a call without a call id never reuses an
  earlier decision;
- the timeout is capped below the runtime's hook timeout, and `nan`, `inf`
  and negative values are refused;
- the post-tool event writes an audit line and never blocks;
- each example config registers every event and blocks on hook failure,
  including the `|| exit 2` guard for Codex and Gemini CLI;
- the shared code is identical across the five Python hooks and across the
  three `client.ts` copies, and the Python hooks import on Python 3.9.

### What they do not confirm

The tests cannot show that a runtime calls the hook, or how it treats the
answer. That was checked by hand, against `tools/mock_governance.py`, with
one shell call (expected: denied) and one file read (expected: approved):

| Agent | Version | Deny and allow | Hook crash |
| --- | --- | --- | --- |
| Claude Code | 2.1.296 | pass | blocked |
| Codex CLI | 0.162.1 | pass | blocked with the guard; runs without it |
| Cursor CLI | 2026.10.14 | pass | blocked |
| Gemini CLI | 0.64.0 | pass | blocked with the guard; runs without it |
| Copilot CLI | 1.0.95 | pass, with user-level hooks | blocked |
| OpenCode | 2.0.25 | pass, through the background service with plugin options | not checked |
| Pi | 1.3.0 | pass | not checked |
| Mastra | 1.77.0, `generate` and `stream` | pass | not checked |
| LangChain | 1.4.6, `invoke` and `ainvoke` | pass | not checked |

The TypeScript plugins also pass `tsc --strict` against the real Pi, OpenCode
(1.19.0 and 2.0.25) and Mastra types. That check is not part of `make test`.

Not checked at all: a real Mezmo Governance endpoint, a reply from Mezmo
that is not valid JSON or lacks `approved`, a decision a person
approves while the hook waits, Windows, the Copilot cloud agent and VS Code,
and how each runtime behaves when the hook times out. To repeat a live check,
start the mock and follow the smoke test in the adapter's README. For Mastra
and LangChain, [examples/check.sh](examples/) runs a real agent and fails if
the gate lets the wrong tool run.

## Security

Report a vulnerability as described in [SECURITY.md](SECURITY.md), not in a
public issue.

Apache License 2.0. See [LICENSE](LICENSE).
