# Claude Agent SDK + Mezmo Governance

Hook callbacks for the Claude Agent SDK, Python (`claude-agent-sdk`) and
TypeScript (`@anthropic-ai/claude-agent-sdk`). Before each tool runs, the
`PreToolUse` callback asks Mezmo Governance. It returns a deny with Mezmo's
reason when Mezmo denies the call or cannot give a decision. After a tool runs,
`PostToolUse` and `PostToolUseFailure` append a local JSONL record.

Tested with Python `claude-agent-sdk` 0.2.168 and TypeScript
`@anthropic-ai/claude-agent-sdk` 0.3.301, both on Claude Code 2.1.301.
Official docs: https://code.claude.com/docs/en/agent-sdk/hooks

## Why callbacks, not the Claude Code hook

The SDK runs Claude Code, so the [Claude Code hook](../claude-code/) would also
work through a settings file. These callbacks are passed in the agent's
options instead, for three reasons:

- The settings file loads only when `setting_sources` includes its source. An
  app that sets `setting_sources=[]` drops the hook without a warning, and
  every tool runs. Options passed in code cannot drop out like that.
- A TypeScript app needs no `python3` on the machine, and no process starts
  for each tool call.
- Mezmo sees `origin.kind` `claude_agent_sdk_hooks`, so the app's calls are
  not mixed up with a person's Claude Code session.

The callbacks reuse the shared clients: `python/.../client.py` is a copy of
the LangChain client, and `typescript/client.ts` is the fourth copy of the
Pi, OpenCode and Mastra client. Drift tests keep the copies the same.

## Install

Set the URL and the service account token in the app's environment:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

### Python

Python 3.10+ and asyncio, the SDK's default event loop:

```bash
pip install ./claude-agent-sdk/python      # from a checkout of this repository
```

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from mezmo_governance_claude_agent_sdk import mezmo_governance_hooks

options = ClaudeAgentOptions(hooks=mezmo_governance_hooks())
async for message in query(prompt="Clean up the build folder", options=options):
    print(message)
```

`ClaudeSDKClient(options=options)` takes the same options.
`mezmo_governance_hooks(endpoint, access_key=...)` takes the URL and key as
arguments, and they win over the variables.
The URL must use `https://`; plain `http://` works only to a loopback
address such as `127.0.0.1`, for a local mock. A URL with a password is refused.

### TypeScript

Node 22+. Copy `typescript/client.ts` and `typescript/mezmo-governance.ts`
into your project:

```typescript
import { query } from "@anthropic-ai/claude-agent-sdk";
import { mezmoGovernanceHooks } from "./mezmo-governance.ts";

for await (const message of query({
  prompt: "Clean up the build folder",
  options: { hooks: mezmoGovernanceHooks },
})) {
  console.log(message);
}
```

`createMezmoGovernanceHooks({ fetchImpl })` builds a fresh set if you need to
inject a custom fetch in tests.

### Your own hooks

The SDK runs every matching hook, and a deny from any of them wins. To add
your own, append to the lists instead of replacing them:

```python
hooks = mezmo_governance_hooks()
hooks["PreToolUse"].append(HookMatcher(matcher="Bash", hooks=[my_check]))
```

## How it fails closed

- The `PreToolUse` matcher has no pattern, so every tool call is checked:
  built-in tools, MCP tools and SDK tools. The SDK docs say hooks also fire
  inside subagents; that was not checked live.
- An approval returns `{}`, so the app's own permission rules still apply.
  Hooks run before permission rules and modes, so a deny holds even in
  `bypassPermissions` mode. The live check runs in that mode, where the hook
  is the only thing that can stop a tool.
- The callbacks catch every error and return a deny. That catch matters: with
  SDK 0.2.168, a `PreToolUse` callback that raises lets the tool run (checked
  by hand). The TypeScript callback catches errors the same way.
- The matcher timeout is 600 seconds and the client's deadline is 590, so the
  agent sees `approval still pending` before Claude Code gives up. If the
  callback still times out, Claude Code skips the tool (checked by hand with a
  2 second timeout).
- Mezmo unreachable, an HTTP error, a reply that is not JSON or has no
  boolean `approved`, an unknown poll status, missing settings and malformed
  hook input all deny.

The Python client bounds each request with `asyncio.wait_for`, so it needs an
asyncio loop. Under trio every call is denied.

## Settings

The same variables as the other plugins: see the [root README](../README.md#settings).
The audit log defaults to `~/.mezmo/governance-audit.jsonl`.
Each line keeps up to 2,000 characters of the tool's result, so the Python
hooks keep the log readable by its owner only: mode 0600, in a 0700 folder.

## What the hooks send

`request_id` is `claude_agent_sdk:<session_id>:<tool_use_id>#<digest>`.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). The tool use id
comes from the callback's second argument, else the input's `tool_use_id`. A
call without either gets a random `generated-<uuid>`, so it never reuses an
earlier decision. An audit line for such a call has `null` ids.

### Tool names for policy

Built-in tools (`Bash`, `Read`, `Write`, `Edit`, `WebFetch` and the rest) use
namespace `claude_agent_sdk`: write policy for `claude_agent_sdk:Bash`. MCP
tools, including tools from `create_sdk_mcp_server`, arrive as
`mcp__<server>__<tool>`: the server is the namespace, so the example's
`delete_note` tool on server `notes` is `notes:delete_note`.

A deny returns:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "Mezmo Governance denied this exact tool call: …"}}
```

The agent sees the reason as the tool result.

## Smoke test

To check fail-closed without a model, run the callback against a port where
nothing listens:

```bash
cd claude-agent-sdk/python
MEZMO_GOVERNANCE_URL=http://127.0.0.1:9/authorize MEZMO_GOVERNANCE_TOKEN=test \
  python -c 'import asyncio
from mezmo_governance_claude_agent_sdk import create_pre_tool_use
event = {"hook_event_name": "PreToolUse", "session_id": "smoke", "tool_name": "Bash",
         "tool_input": {"command": "echo hi"}, "tool_use_id": "smoke-1"}
print(asyncio.run(create_pre_tool_use()(event, "smoke-1", {"signal": None})))'
```

It prints a deny that says `could not authorize`. With your real URL and key
set, the same command prints `{}` when Mezmo allows the call. Use a new
`tool_use_id` each run: Mezmo returns the earlier decision for an id it has
seen.

With a real model, [examples/check.sh](../examples/) `claude-agent-sdk` runs
an agent against the local mock and fails if the gate lets the wrong tool run.

## Tests

From this directory:

```bash
python3 -m venv python/.venv && python/.venv/bin/pip install -r python/requirements.txt
(cd python && PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v)
(cd typescript && node --test test/*.test.ts)
```

From the repo root: `make test-claude-agent-sdk`. Without the venv, the
Python tests that need `httpx` or the SDK skip.
