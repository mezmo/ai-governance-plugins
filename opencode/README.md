# OpenCode + Mezmo Governance

An OpenCode plugin that throws before a tool runs when Mezmo Governance
denies the call (or cannot be reached). The after hook only appends a local
JSONL record.

One file serves both majors. Its default export carries `server` for
OpenCode 1.x and `setup` for OpenCode 2.x; each version ignores the other key.

| OpenCode | Install | Settings from | Block hook |
| --- | --- | --- | --- |
| 1.x, tested 1.19.0 | `mezmo-governance.ts` and `mezmo-governance/` in `plugins/` | environment | `tool.execute.before` |
| 2.x, tested 2.0.25 | this folder, named in `opencode.json` `plugins` | plugin options | `tool.hook("execute.before")` |

Tested with a type check against `@opencode-ai/plugin` 1.19.0 and
`@opencode/plugin` 2.0.25, tests with fake runtimes that follow each
version's loader and hook call sites, and a load check on the real 1.19.0
and 2.0.25 binaries. On 2.x, a live `opencode run` through the background
service, against `tools/mock_governance.py`, denied `shell` and allowed
`read`.

Official docs: https://opencode.ai/docs/plugins/

## Background service (2.x)

OpenCode 2.x runs tools in a background service (`opencode serve
--service`). The service starts once, on its own, so it does not see the
variables you export in the shell that runs `opencode`. With only
environment variables, every call is blocked with
`MEZMO_GOVERNANCE_URL is required`: safe, but nothing runs. Use plugin
options on 2.x. `opencode run --standalone` starts a private server from
your shell, so the environment works there.

Plugin options use the variable names as keys and win over the environment.
OpenCode 2.x passes options only to a plugin folder named in `opencode.json`,
not to files it finds in `plugins/`.

## Install on 2.x

`./mezmo-governance install opencode` from the repository root does this for you, and `uninstall` undoes it. The steps below are the manual way.

1. Copy this folder out of any `plugins/` folder, and put the key in a file
   only you can read:

   ```bash
   cp -R opencode ~/.config/opencode/mezmo-governance
   mkdir -p ~/.config/mezmo
   printf '%s\n' '<service account token>' > ~/.config/mezmo/governance-key
   chmod 600 ~/.config/mezmo/governance-key
   ```

2. Name the folder in `~/.config/opencode/opencode.json` (or a project's
   `opencode.json`; a relative path starts at that file's folder):

   ```json
   {
     "plugins": [
       {
         "package": "./mezmo-governance",
         "options": {
           "MEZMO_GOVERNANCE_URL": "https://api.mezmo.com/governance/workflow/authorize",
           "MEZMO_GOVERNANCE_TOKEN": "{file:~/.config/mezmo/governance-key}"
         }
       }
     ]
   }
   ```

   OpenCode replaces `{file:...}` with the file's content, so the key stays
   out of the config. A missing file becomes an empty key, which blocks every
   call. `index.ts` is the folder's entry. Do not also copy the plugin into
   `plugins/`: OpenCode would load it twice, and the copy without options
   blocks every call.

## Install on 1.x

Copy the plugin file and its `mezmo-governance/` folder into a plugin
directory OpenCode loads automatically:

```bash
mkdir -p ~/.config/opencode/plugins
cp -R mezmo-governance.ts mezmo-governance ~/.config/opencode/plugins/
```

Or copy both into `.opencode/plugins/` of a project. OpenCode loads every
`plugins/*.ts` file at startup as a plugin, so keep `client.ts` inside the
`mezmo-governance/` folder: as a top-level file it fails to load. If an
earlier install left `plugins/client.ts` behind, delete it. Do not copy
`index.ts` there: it would load the plugin a second time.

Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

## Verify it works

1. From the plugin directory, check the URL, the key and your policy. This
   prints the decision for one shell call, or why there is none (Node 22.18+):

   ```bash
   node --input-type=module -e 'import { approvalPayload, authorize } from "./mezmo-governance/client.ts";
   authorize(approvalPayload({ sessionId: "verify", toolCallId: crypto.randomUUID(), toolName: "shell", arguments: { command: "ls" } }))
     .then(console.log, (error) => console.log("no decision:", error.message));'
   ```

2. Check that OpenCode blocks when Mezmo cannot answer. Set the URL to a
   closed port, such as `http://127.0.0.1:9/authorize` (on 2.x in the plugin
   options, on 1.x in `MEZMO_GOVERNANCE_URL`), and ask for a tool call:
   `opencode run "Run ls in the shell"`. The tool error must say
   `Mezmo Governance could not authorize this exact tool call, so OpenCode
   blocked it`. If `ls` output appears, the plugin did not load.

3. With the real URL, run any tool, then read the last audit line:
   `tail -1 ~/.mezmo/governance-audit.jsonl`.

On OpenCode 2.x, `opencode plugin list` shows `mezmo-governance` in the ID
column once the plugin has loaded. An ID of `-` means it failed to load, and
tools run ungated. Right after the server starts, the list can still be
empty; run it again.

## Tool names for policy

OpenCode sends built-in tools in namespace `opencode`. The shell tool is
`shell` on 2.x and `bash` on 1.x; files are read with `read`. Live on 2.x,
the plugin sent `opencode:shell` and `opencode:read`. Write policy for both
shell names if you run both majors.

## What it sends

`request_id` is `opencode:<sessionID>:<callID>#<digest>` on 1.x.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). OpenCode 2.x Code
Mode runs every inner tool call of one `execute` call under that call's id,
so on 2.x each call sends `<callID>#<random UUID>#<digest>`: no two calls share a
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
name) after Mezmo approved them. Load this plugin last: a plugin that
rewrites tool input after it runs input Mezmo never approved. Install only
plugins you trust next to this one. When what ran differs from what was approved, the audit line carries
`"arguments_changed": true`.

Do not also rely on `permission.ask` for this gate on 1.x. That hook is not
triggered on every 1.x release; `tool.execute.before` is the one that can
throw and stop the call.

## Tests

Node 22.18+:

```bash
node --test test/*.test.ts
```
