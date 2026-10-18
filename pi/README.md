# Pi + Mezmo Governance

A Pi coding-agent extension that blocks on Mezmo Governance in `tool_call` and
writes a local JSONL record in `tool_result`. Handler errors on `tool_call`
also block (Pi fail-safe).

Tested with Pi 1.3.0: a live `pi -p -e` run against `tools/mock_governance.py`
(see below). The type check against its `ExtensionAPI` and the tests with a
fake runtime used `@earendil-works/pi-coding-agent` 1.2.0. Pi moved from
`@mariozechner/pi-coding-agent` to `@earendil-works/pi-coding-agent`. The
`tool_call` and `tool_result` fields this extension reads are the same in
`@mariozechner/pi-coding-agent` 0.73. The extension imports neither package.

Official docs: https://pi.dev/docs/latest/extensions

## Install

`./mezmo-governance install pi` from the repository root runs `pi install` on this folder, and `uninstall` runs `pi remove`. Pi then loads the extension from the checkout. The steps below copy it instead.

By hand, from the repository root: `pi install ./pi`. Or copy the extension
into its own folder under a trusted extension path:

```bash
mkdir -p ~/.pi/agent/extensions/mezmo-governance
cp pi/mezmo-governance.ts pi/client.ts pi/package.json ~/.pi/agent/extensions/mezmo-governance/
```

Keep the files in that folder. Pi loads every `.ts` file directly under
`extensions/` as an extension, and `client.ts` is not one. Inside the folder,
`package.json` (`"pi": { "extensions": ["./mezmo-governance.ts"] }`) names
the entry point, so `pi install` of the folder also works.

Project-local: `.pi/extensions/mezmo-governance/` after the project is trusted.
One session only: `pi -e ./mezmo-governance.ts`.

Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

## Verify it works

1. From this folder (Node 22.18+), check the URL, the key and your policy. This prints the
   decision for one `bash` call, or why there is none:

   ```bash
   node --input-type=module -e 'import { approvalPayload, authorize } from "./client.ts";
   authorize(approvalPayload({ sessionId: "verify", toolCallId: crypto.randomUUID(), toolName: "bash", arguments: { command: "ls" } }))
     .then(console.log, (error) => console.log("no decision:", error.message));'
   ```

2. Check that Pi blocks when Mezmo cannot answer. Point the extension at a
   closed port and ask for a tool call:

   ```bash
   MEZMO_GOVERNANCE_URL=http://127.0.0.1:9/authorize pi -p "Run ls in the shell"
   ```

   The tool result must say `Mezmo Governance could not authorize this exact
   tool call, so Pi blocked it`. If `ls` output appears, the extension did not
   load.

3. With the real URL, run any tool, then read the last audit line:
   `tail -1 ~/.mezmo/governance-audit.jsonl`.

## Load order

Pi runs `tool_call` handlers in load order: project `.pi/extensions/`, then
`~/.pi/agent/extensions/`, then `-e` paths. A handler may change
`event.input` in place, so an extension that loads after this one can change
the arguments after Mezmo approved them. Load this extension last, for
example with `-e`: an extension that rewrites tool input after it runs input
Mezmo never approved. Install only extensions you trust next to this one. When the arguments that ran differ from the approved ones,
the audit line carries `"arguments_changed": true`.

## What it sends

The extension calls `ctx.ui.setStatus("mezmo-governance", ...)` while waiting.
`request_id` is `pi:<sessionId>:<toolCallId>#<digest>`. Without a Pi session
id, `<sessionId>` is `ephemeral-` plus a random id made once per process, so an
approval never carries over to the next run.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). Built-in tools use namespace
`pi`. Names of the form `mcp__<server>__<tool>` split the same way as Codex.

### Tool names for policy

Pi's built-in tools are `bash`, `powershell`, `read`, `edit`, `write`,
`grep`, `find` and `ls`, in namespace `pi`. A live `pi -p -e` run with Pi
1.3.0 against `tools/mock_governance.py` sent `pi:bash` and `pi:read`.

## Troubleshooting

`doctor` does not check this library: its setup lives in app code. The
[root table](../README.md#troubleshooting) lists each Mezmo error and its fix.
`MEZMO_GOVERNANCE_URL is required` or `must be a non-empty string` means the
process that runs the agent lacks the variables.

## Tests

Node 22.18+ (type stripping):

```bash
node --test test/*.test.ts
```

`test/drift.test.ts` compares this `client.ts` with the OpenCode, Mastra and
Claude Agent SDK copies. It skips when those directories are absent.

From the repo root: `make test-pi`.
