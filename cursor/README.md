# Cursor + Mezmo Governance

A Cursor agent hook that asks Mezmo Governance before a tool runs. The example
config uses `preToolUse` so every tool type is gated once (`Shell`, `Read`,
`Write`, MCP, Task). Do not also register `beforeShellExecution` /
`beforeMCPExecution` on the same script unless you want a second authorize call
for those tools.

Official docs: https://cursor.com/docs/hooks

The script needs Python 3.9 or later and uses only the standard library.

Tested version: Cursor CLI (`cursor-agent`) 2026.10.14, run live against
`tools/mock_governance.py`: a shell call was denied, a read was approved, and
a hook crash blocked the call. The Cursor editor was not run.

## Fail behavior

Keep `failClosed: true` on the gating hook. Without it, Cursor lets the action
through when the hook crashes, times out, exits with a code other than 0 or 2,
or prints nothing.

Cursor still runs tools ungoverned when:

- `failClosed` is missing from the `preToolUse` entry;
- a cloud agent runs in a project without the hook in `.cursor/hooks.json`:
  cloud agents do not load `~/.cursor/hooks.json`.

## Install

`./mezmo-governance install cursor` from the repository root does this for you, and `uninstall` undoes it. The steps below are the manual way.
Run them from the repository root. Cursor has no override for the
`~/.cursor` folder.

1. Copy the hook:

```bash
mkdir -p ~/.cursor/hooks
cp cursor/mezmo_governance_hook.py ~/.cursor/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into `~/.cursor/hooks.json`
   or `.cursor/hooks.json`. The example path starts at `$HOME`, so it works
   from any directory. Cursor runs user hooks from `~/.cursor/` and project
   hooks from the project root, so a relative path points at different files.
   To keep the script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute
   path. To run the script without `python3` in front, run `chmod +x` on it
   first.

3. Export the settings in the shell that starts Cursor:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

   Start Cursor from that shell (`cursor-agent`, or `cursor .` for the
   editor). Windsurf, another VS Code-based editor, passed no variables to the
   hook when started from the Dock, so every call was denied with
   `MEZMO_GOVERNANCE_URL must be a non-empty string`. The Cursor editor may do
   the same; this is not verified.

4. Run the checks in [Verify](#verify), then one tool call your policy denies.

Optional matchers: `Shell`, `Read`, `Write`, `MCP:<tool_name>`. MCP lifecycle
hooks are deferred in cloud agents; `preToolUse` is the safer gate.

If you prefer the MCP-specific events, the script also handles
`beforeMCPExecution` / `afterMCPExecution` (`tool_input` may be a JSON string,
`mcp_server_name` becomes `tool_namespace`) and `beforeShellExecution` /
`afterShellExecution` (tool name `Shell`, arguments `{ "command": "..." }`).

## Verify

From the repository root, with the two variables exported:

```bash
./mezmo-governance doctor cursor
./mezmo-governance smoke cursor
```

`doctor` checks the install, the settings and the key. `smoke` runs the
installed hook against its own mock: one shell call must be denied and one
read approved. Neither starts the agent.

To check that the agent calls the hook, run
`./mezmo-governance mock --deny Shell` in a second terminal. In the agent's
terminal, export the two lines it prints, start the agent, and ask it to run
`ls`. The
mock prints a `denied` line for `cursor:Shell`. No line means the hook did not
run.

## Smoke test

Run these from the `cursor/` folder. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"preToolUse","conversation_id":"smoke","tool_use_id":"smoke-1","tool_name":"Shell","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"permission":"deny","user_message":"Mezmo Governance could not authorize this exact tool call, so Cursor blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>","agent_message":"Mezmo Governance could not authorize this exact tool call, so Cursor blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}
```

Then check your key with the variables above set. This asks Mezmo about one
`echo hi` call:

```bash
echo '{"hook_event_name":"preToolUse","conversation_id":"smoke","tool_use_id":"smoke-2","tool_name":"Shell","tool_input":{"command":"echo hi"}}' \
  | python3 mezmo_governance_hook.py
```

`{"permission":"allow"}` means Mezmo allowed the call. A deny prints the shape
above with your policy's reason. Use a new `tool_use_id` for each run: Mezmo
returns the earlier decision for an id it has seen.

## MCP tools under preToolUse

Cursor's docs give the `MCP:<tool_name>` matcher for `preToolUse`, but not the
input fields for an MCP call. This is not verified. The hook reads
`MCP:<tool>` or `mcp__<server>__<tool>` as the tool name, and
`mcp_server_name` as the namespace when present. Without a server name, an MCP
call goes to Mezmo with namespace `mcp`, so a policy keyed on the server
name does not match it. To gate MCP tools by server, use `beforeMCPExecution`,
which always sends `mcp_server_name`. The hook denies a call from an MCP server
named `cursor`: its tools would pass as built-ins.

## Windows

Cursor's docs do not say which shell runs a hook command on Windows. The
example uses POSIX shell syntax (`${VAR:-default}`, `$HOME`) and `python3`. On
Windows, use an absolute path:

```json
"command": "py -3 C:/Users/<you>/.cursor/hooks/mezmo_governance_hook.py"
```

In PowerShell, set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_TOKEN = "..."` before you start Cursor.

## What the hook sends

Allow:

```json
{ "permission": "allow" }
```

Deny:

```json
{
  "permission": "deny",
  "user_message": "Mezmo Governance denied this exact tool call: …",
  "agent_message": "Mezmo Governance denied this exact tool call: …"
}
```

`request_id` is `cursor:<conversation_id>:<tool_use_id>#<digest>`.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). The shell and MCP
events carry no `tool_use_id`, so the hook uses a fresh `run-<uuid>` for each
run. A repeated identical command then gets its own decision instead of
reusing an earlier one. Their post-tool records have `decision_id: null`.

Post-tool events only append the local JSONL audit log. Cursor sends
`tool_output` and `result_json` as JSON text; the hook parses it to spot a
failed call. The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

## Troubleshooting

Run `./mezmo-governance doctor --probe cursor` from the repository root.
[Troubleshooting](../README.md#troubleshooting) in the root README lists each
error and its fix. For Cursor, check `failClosed: true` on the `preToolUse` entry. A cloud agent loads only the project `.cursor/hooks.json`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-cursor`.
