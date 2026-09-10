# Pi + Mezmo Governance

A Pi coding-agent extension that blocks on Mezmo Governance in `tool_call` and
writes a local JSONL record in `tool_result`. Handler errors on `tool_call`
also block (Pi fail-safe).

Official docs: https://pi.dev/docs/latest/extensions

## Install

Copy this directory (or just the TypeScript files) into a trusted extension
path:

```bash
mkdir -p ~/.pi/agent/extensions/mezmo-governance
cp mezmo-governance.ts client.ts package.json ~/.pi/agent/extensions/mezmo-governance/
```

Project-local: `.pi/extensions/mezmo-governance/` after the project is trusted.
Quick test: `pi -e ./mezmo-governance.ts`.

Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

`package.json` has `"pi": { "extensions": ["./mezmo-governance.ts"] }` so
`pi install` of this folder also loads it.

The extension calls `ctx.ui.setStatus("mezmo-governance", ...)` while waiting.
`request_id` is `pi:<sessionId>:<toolCallId>`. Built-in tools use namespace
`pi`. Names of the form `mcp__<server>__<tool>` split the same way as Codex.

## Tests

Node 22+ (type stripping):

```bash
node --test test/client.test.ts
```
