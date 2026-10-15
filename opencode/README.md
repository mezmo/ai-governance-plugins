# OpenCode + Mezmo Governance

An OpenCode 1.x plugin that throws in `tool.execute.before` when Mezmo
Governance denies a call (or cannot be reached). `tool.execute.after` only
appends a local JSONL record.

Tested against OpenCode 1.19.0: a type check against `@opencode-ai/plugin`
1.19.0, plus tests with a fake runtime that follow the 1.19.0 plugin loader
and hook call sites.

OpenCode 2.x is not supported. Its plugins default-export `{ id, setup }` and
register `ctx.tool.hook("execute.before", ...)`. OpenCode 2.x rejects this
plugin's module shape at load time, so tools run ungated. Check with
`opencode --version` before you install.

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
earlier install left `plugins/client.ts` behind, delete it.

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

## What it sends

`request_id` is `opencode:<sessionID>:<callID>`. Built-in tools use namespace
`opencode`. Names of the form `mcp__<server>__<tool>` split into that server
and tool. OpenCode MCP naming is not uniform across versions; unknown names stay
in namespace `opencode`.

The audit line is `failed` when the shell exit code (`metadata.exit`) is
non-zero, or null because the command was killed. It is also `failed` when an
MCP result has `isError`, or when the tool returned no result.

OpenCode runs plugin hooks in load order and hands each the same `args`
object, so a plugin that runs after this one can change the arguments after
Mezmo approved them. Install only plugins you trust next to this one. When
the arguments that ran differ from the approved ones, the audit line carries
`"arguments_changed": true`.

Do not also rely on `permission.ask` for this gate. That hook is not triggered
on every OpenCode release; `tool.execute.before` is the one that can throw and
stop the call.

## Tests

Node 22+:

```bash
node --test test/*.test.ts
```
