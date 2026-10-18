# Antigravity CLI + Mezmo Governance

An Antigravity CLI (`agy`) command hook that asks Mezmo Governance before each
tool runs. If Mezmo denies the call, or the hook cannot get an answer, the hook
denies and Antigravity skips the tool. After a tool finishes, the same script
appends a local JSONL record.

Antigravity CLI replaces Gemini CLI for most users. Gemini Code Assist
Enterprise users keep Gemini CLI: use [gemini-cli](../gemini-cli/README.md)
there.

The script needs Python 3.9 or later and uses only the standard library.

Tested versions: Antigravity CLI 1.4.0, and 1.4.1 with
`~/.gemini/config/hooks.json`, the path this README installs, against
production Mezmo, on 2026-10-17.

Run live in `agy -p` 1.4.0 on 2026-10-17 against `tools/mock_governance.py`, with
the hook in a workspace `.agents/hooks.json`: `run_command` (`touch marker`) was
denied and no file was written; `view_file` was approved. With
`MEZMO_GOVERNANCE_HOOK` pointed at a script that raises, or at a missing file,
the command was blocked too. The hook fields, file locations and fail behavior
below come from those runs and from the hooks guide built into the 1.4.0
binary.

## Fail behavior

Antigravity CLI 1.4.0 blocks the tool when a `PreToolUse` hook exits non-zero,
crashes, prints something that is not JSON, or reaches its `timeout`. Each was
run live. So the command needs no `|| exit 2` guard.

The `timeout` is in seconds and defaults to 30. The example sets the
`PreToolUse` `timeout` to `600`, so the script's own 590 second deadline ends
first and returns a clear deny. Antigravity CLI honored `600` live on 2026-10-17:
against a mock that held the call, the script's deadline ended first and
blocked it with `approval still pending` after 602 seconds. Keep the `timeout` at least 10 seconds above
`MEZMO_GOVERNANCE_TIMEOUT_SECONDS`.

Antigravity still runs the tool when:

- the named hook has `"enabled": false`;
- `hooks.json` holds an entry Antigravity cannot read, such as a `timeout`
  that is not a whole number or a hook `type` other than `command` or
  `prompt`. Antigravity then drops the whole file, every hook in it, and only
  logs it. `install` refuses such a file, and `doctor` fails it.

`--dangerously-skip-permissions` does not skip hooks in 1.4.0: a `PreToolUse`
deny still blocked the tool live. A report that a deny on `invoke_subagent` is
ignored (google-antigravity/antigravity-cli#640) did not reproduce in 1.4.0:
the deny stopped the subagent. A subagent's own tool calls go through the same
hooks, with the subagent's own `conversationId`.

Run the [smoke test](#smoke-test) after each install, and run
`agy -p /hooks` after each config change.

## Install

`./mezmo-governance install antigravity` from the repository root does this
for you, and `uninstall` undoes it. It refuses, and changes nothing, when
`hooks.json` already has a `mezmo-governance` hook that runs no Mezmo script,
or holds an entry that would make Antigravity drop the file. The steps below
are the manual way. Run them from the repository root. Antigravity CLI has
no override for the `~/.gemini/config` folder.

1. Copy the hook:

```bash
mkdir -p ~/.gemini/config/hooks
cp antigravity/mezmo_governance_hook.py ~/.gemini/config/hooks/
```

2. Add the `mezmo-governance` entry from
   [hooks.example.json](hooks.example.json) to `~/.gemini/config/hooks.json`.
   That file maps a hook name to its events; keep the other names in it. The
   example path starts at `$HOME`, so it works from any directory. To keep the
   script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path.

   Antigravity also reads `<workspace>/.agents/hooks.json` and a plugin's
   `hooks.json`, and runs the hooks of every file for the same event. A
   workspace entry with the user entry's name is ignored, so it cannot turn
   the user hook off (`agy -p /hooks`, 1.4.0). When one hook allows a call and
   another denies it, the tool is blocked, in either order (live, 1.4.0).

3. Export the URL and the service account token. The token is a standard
   Mezmo service key, not a key of the agent; see
   [Connect to Mezmo](../README.md#connect-to-mezmo):

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Run `agy -p /hooks` and check that it lists `mezmo-governance` with
   `PreToolUse` and `PostToolUse`, both `enabled`. It starts no agent turn.

Antigravity inherits the environment of the shell that launches it, and runs
each hook with `sh -c` in the folder that holds its `hooks.json`. On Windows
its guide says it uses `cmd /c`, which does not expand `${...}`: the example
command would fail there and block every tool. Windows is not verified.

To gate only some tools, set `matcher`, a regex on the tool name such as
`run_command|write_to_file`. `*` or an empty matcher matches every tool.

## Verify

From the repository root, with the two variables exported:

```bash
./mezmo-governance doctor antigravity
./mezmo-governance smoke antigravity
```

`doctor` checks the install, the settings and the key. `smoke` runs the
installed hook against its own mock: one shell call must be denied and one
read approved. Neither starts the agent.

To check that the agent calls the hook, run
`./mezmo-governance mock --deny run_command` in a second terminal. In the agent's
terminal, export the two lines it prints, start the agent, and ask it to run
`ls`. The
mock prints a `denied` line for `antigravity:run_command`. No line means the hook did not
run.

## Smoke test

Run these from the `antigravity/` folder. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"conversationId":"smoke","toolCall":{"name":"run_command","args":{"CommandLine":"echo hi"}}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py PreToolUse
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"decision":"deny","reason":"Mezmo Governance could not authorize this exact tool call, so Antigravity CLI blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}
```

Then check your key with the variables from step 3 set. This asks Mezmo about
one `echo hi` call:

```bash
echo '{"conversationId":"smoke","toolCall":{"name":"run_command","args":{"CommandLine":"echo hi"}}}' \
  | python3 mezmo_governance_hook.py PreToolUse
```

No output means Mezmo allowed the call. A deny prints the shape above with your
policy's reason.

## Tool names in policy

Mezmo sees the Antigravity tool name with namespace `antigravity`. Names seen
live in 1.4.0: `run_command` for shell commands, `view_file`, `write_to_file`,
`replace_file_content`, `invoke_subagent` and `send_message`. Other tool names
are not verified.

An MCP call reaches the hook as `call_mcp_tool` with `ServerName`, `ToolName`
and `Arguments`. The hook sends it with the server as the namespace, the
server's tool name, and the tool's own arguments. The hook denies a call to an
MCP server named `antigravity`: its tools would pass as built-ins.

## What the hook sends

The payload names no event, so the `hooks.json` command passes `PreToolUse` or
`PostToolUse` as the script's first argument. Without it the hook denies.

Antigravity sends `conversationId` and the step number `stepIdx`, but no tool
call id. A rewound conversation can reuse a step number, so `request_id` is
`antigravity:<conversationId>:run-<uuid>#<digest>`, new for every run. A
repeated identical call opens a new decision instead of reusing an earlier one
through HTTP 409 ([identity](../docs/protocol.md#identity)). The post-tool
record has `decision_id` and `request_id` set to `null` for the same reason.

Built-in tool arguments go to Mezmo as Antigravity sends them, including the
model's `toolAction` and `toolSummary` notes.

PreToolUse deny stdout:

```json
{"decision": "deny", "reason": "Mezmo Governance denied this exact tool call: …"}
```

An approved call prints nothing, so Antigravity's own permission rules still
apply. If Mezmo is down, JSON is malformed, or the deadline expires while the
decision is still pending, the hook denies. On input it cannot read, such as
broken or too deeply nested JSON, or a missing or unknown event argument, it
prints a deny and exits 2.

The `PostToolUse` payload holds no tool result, only an `error` text when the
tool failed. The record is `failed` with that text as its preview, else
`observed` with an empty preview.

## Troubleshooting

Run `./mezmo-governance doctor --probe antigravity` from the repository root.
[Troubleshooting](../README.md#troubleshooting) in the root README lists each
error and its fix. For Antigravity CLI, run `agy -p /hooks` and check that the hook is not `"enabled": false`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-antigravity`.
