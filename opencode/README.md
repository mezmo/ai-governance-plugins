# OpenCode + Mezmo Governance

An OpenCode plugin that throws in `tool.execute.before` when Mezmo Governance
denies a call (or cannot be reached). `tool.execute.after` only appends a local
JSONL record.

Official docs: https://opencode.ai/docs/plugins/

## Install

Copy the plugin file and its `mezmo-governance/` folder into a plugin
directory OpenCode loads automatically:

```bash
mkdir -p ~/.config/opencode/plugins
cp -R mezmo-governance.ts mezmo-governance ~/.config/opencode/plugins/
```

Or copy both into `.opencode/plugins/` of a project. OpenCode loads every
`plugins/*.ts` file at startup as a plugin, so keep `client.ts` inside the
`mezmo-governance/` folder: as a top-level file it fails to load.

Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

`request_id` is `opencode:<sessionID>:<callID>`. Built-in tools use namespace
`opencode`. Names of the form `mcp__<server>__<tool>` split into that server
and tool. OpenCode MCP naming is not uniform across versions; unknown names stay
in namespace `opencode`.

Do not also rely on `permission.ask` for this gate. That hook is not triggered
on every OpenCode release; `tool.execute.before` is the one that can throw and
stop the call.

## Tests

Node 22+:

```bash
node --test test/client.test.ts
```
