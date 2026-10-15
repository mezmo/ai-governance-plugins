# OpenCode + Mezmo Governance

An OpenCode plugin that throws before a tool runs when Mezmo Governance
denies the call (or cannot be reached). The after hook only appends a local
JSONL record.

One file serves both majors. Its default export carries `server` for
OpenCode 1.x and `setup` for OpenCode 2.x; each version ignores the other key.

| OpenCode | Copy | Entry it calls | Block hook |
| --- | --- | --- | --- |
| 1.x, tested 1.19.0 | `mezmo-governance.ts` and `mezmo-governance/` | `server` | `tool.execute.before` |
| 2.x, tested 2.0.25 | the same two | `setup` | `tool.hook("execute.before")` |

Tested with a type check against `@opencode-ai/plugin` 1.19.0 and
`@opencode/plugin` 2.0.25, tests with fake runtimes that follow each
version's loader and hook call sites, and a load check on the real 1.19.0
and 2.0.25 binaries.

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
`mezmo-governance/` folder: as a top-level file it fails to load. If an
earlier install left `plugins/client.ts` behind, delete it. OpenCode 2.x also
loads a folder that holds a `server` or `index` file, so add neither to
`mezmo-governance/`.

Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

## Verify it works

1. From the plugin directory, check the URL, the key and your policy. This
   prints the decision for one `bash` call, or why there is none (Node 22+):

   ```bash
   node --input-type=module -e 'import { approvalPayload, authorize } from "./mezmo-governance/client.ts";
   authorize(approvalPayload({ sessionId: "verify", toolCallId: crypto.randomUUID(), toolName: "bash", arguments: { command: "ls" } }))
     .then(console.log, (error) => console.log("no decision:", error.message));'
   ```

2. Check that OpenCode blocks when Mezmo cannot answer. Point the plugin at a
   closed port and ask for a tool call:

   ```bash
   MEZMO_GOVERNANCE_URL=http://127.0.0.1:9/authorize opencode run "Run ls in the shell"
   ```

   The tool error must say `Mezmo Governance could not authorize this exact
   tool call, so OpenCode blocked it`. If `ls` output appears, the plugin did
   not load.

3. With the real URL, run any tool, then read the last audit line:
   `tail -1 ~/.mezmo/governance-audit.jsonl`.

On OpenCode 2.x, `opencode plugin list` shows `mezmo-governance` in the ID
column once the plugin has loaded. An ID of `-` for this file means it failed
to load, and tools run ungated. Right after the server starts, the list can
still be empty; run it again.

## What it sends

`request_id` is `opencode:<sessionID>:<callID>` on 1.x. OpenCode 2.x Code
Mode runs every inner tool call of one `execute` call under that call's id,
so on 2.x each call sends `<callID>#<random UUID>`: no two calls share a
decision. Built-in tools use namespace
`opencode`. Names of the form `mcp__<server>__<tool>` split into that server
and tool. OpenCode MCP naming is not uniform across versions; unknown names stay
in namespace `opencode`.

The audit line is `failed` when the shell exit code (`metadata.exit`) is
non-zero, or null because the command was killed. It is also `failed` when an
MCP result has `isError`, when the tool returned no result, or on 2.x when
the call ended with `status: "error"`.

OpenCode runs plugin hooks in load order and hands each the same event, so a
plugin that runs after this one can change the arguments (and on 2.x the tool
name) after Mezmo approved them. Install only plugins you trust next to this
one. When what ran differs from what was approved, the audit line carries
`"arguments_changed": true`.

Do not also rely on `permission.ask` for this gate on 1.x. That hook is not
triggered on every 1.x release; `tool.execute.before` is the one that can
throw and stop the call.

## Tests

Node 22+:

```bash
node --test test/*.test.ts
```
