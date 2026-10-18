# GitHub Copilot + Mezmo Governance

A GitHub Copilot command hook that asks Mezmo Governance before each tool
runs. If Mezmo denies the call, or the hook cannot get an answer, the hook
prints a deny and Copilot skips the tool. After a tool finishes, the same
script appends a local JSONL record.

It works with Copilot CLI, Copilot cloud agent, and VS Code sessions on
either harness, Copilot or Local. See [VS Code](#vs-code).

Official docs: https://docs.github.com/en/copilot/reference/hooks-configuration

The script needs Python 3.9 or later and uses only the standard library.

Tested versions: Copilot CLI 1.0.95, and 1.0.98 for deny and allow, also with
`--autopilot --allow-all`, run live with `copilot -p` against
`tools/mock_governance.py`.

## Fail behavior

Copilot fails closed for a `preToolUse` command hook that crashes, cannot
start, or exits non-zero: the tool does not run. A crash was verified live
("hook errored"). It fails open on a timeout:
the call goes on to the normal permission flow. The example sets
`timeoutSec` to `600`, so the script's own 590 second deadline ends first and
returns a clear deny. Keep `timeoutSec` at least 10 seconds above
`MEZMO_GOVERNANCE_TIMEOUT_SECONDS`: if you lower one, lower the other too, or
a call still pending runs the tool.

`disableAllHooks: true` in a hooks file or in repository `settings.json` turns
the hook off.

The `preToolUse` command ends with an `|| exit 2` guard, for VS Code Local:
Local runs the tool when a hook exits with a code other than 0 or 2. Copilot
blocks on any non-zero exit, so the guard changes nothing there.

## Install

`./mezmo-governance install copilot` from the repository root does this for you, and `uninstall` undoes it. The steps below are the manual way.

Install the hook at user level, in `~/.copilot/hooks/` or
`$COPILOT_HOME/hooks/`. Copilot CLI 1.0.95 skipped a repository
`.github/hooks/*.json` file in an untrusted folder without any warning: under
`copilot -p` the hook did not run, and `ls` ran ungoverned. The user-level
file ran in the same folder.

To confirm the hook runs, start `python3 tools/mock_governance.py --deny bash`
from this repository, point `MEZMO_GOVERNANCE_URL` at it, and ask Copilot to
run `ls`. The mock prints a `denied copilot:bash` line for each call it sees.
No line means the hook did not run.

1. Copy the hook:

```bash
mkdir -p ~/.copilot/hooks
cp mezmo_governance_hook.py ~/.copilot/hooks/
```

2. Copy [hooks.example.json](hooks.example.json) to
   `~/.copilot/hooks/mezmo-governance.json`. The example path starts at
   `$HOME/.copilot`, so it works from any directory. With `COPILOT_HOME` set,
   or to keep the script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path.

3. Export the settings in the shell that starts `copilot`:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

Each command passes the event name as its last argument, for example
`preToolUse`. The hook needs it: the camelCase payload does not name the event.
Use the camelCase event names only. Copilot CLI 1.0.98 runs a `PreToolUse`
entry as well as a `preToolUse` one from the same file, so it would ask Mezmo
twice for each call, and a `PreToolUse` entry gets Claude's tool names (`Bash`,
not `bash`). The hook reads that payload, but `doctor` flags the entry.

## Tool names in policy

Mezmo sees every tool with namespace `copilot`, also from VS Code Local, so a
rule for every `copilot` tool covers Local too. Names seen live in Copilot CLI
1.0.95: `bash` for shell commands and `view` for reads. VS Code Local uses its
own names, such as `run_in_terminal` (see [VS Code](#vs-code)). In Autopilot, Copilot
also sends its `task_complete` tool through the hook, so a policy that denies
every tool stops Autopilot from finishing.

## VS Code

VS Code reads `~/.copilot/hooks/*.json` for both harnesses. Pick one with the
**Session Target** control in a new chat.

- The Copilot harness runs the hook as Copilot CLI does. Seen live in VS Code
  1.142.0 with Copilot Chat 0.70.0: the mock logged `copilot:bash` and
  `copilot:view`.
- The Local harness maps `preToolUse` to `PreToolUse` and runs the `bash`
  command on macOS and Linux, but sends its own payload (`hook_event_name`,
  `session_id`, `tool_name`, `tool_input`, `tool_use_id`). It obeys a deny
  only in `hookSpecificOutput`. The hook reads that payload and prints its deny
  both at the top level and in `hookSpecificOutput`.

Before this support, Local ran every tool ungoverned: the hook got the Local
payload, could not find `toolName`, and printed a top-level deny that Local
ignored. Seen live in VS Code 1.142.0 on 2026-10-17: a Local session ran
`touch marker`, and the mock got no call.

Local's `tool_use_id` makes the request id stable for a retried call, and joins
each post-tool record to its decision. `chat.useHooks`, on by default, turns
Local hooks on. Local reads `timeoutSec` as its timeout and, like Copilot,
runs the tool when the hook times out, so the 590 second deadline must end
first. Local has no `postToolUseFailure` event, so it logs no failed calls.

Seen live with this hook in VS Code 1.142.0 on 2026-10-17: a Local session's
`touch marker` was denied (`mock policy: copilot:run_in_terminal is denied`, no
file written), and `read_file` was approved. Not verified: on Windows, Local runs the `powershell` command through Windows
PowerShell `-Command`; that is not checked.

## Cloud agent

The cloud agent reads only `.github/hooks/*.json` in the repository. Commit
the script as `.github/hooks/mezmo_governance_hook.py`, and point the `bash`
command at it from the repository root:

```json
"bash": "python3 .github/hooks/mezmo_governance_hook.py preToolUse"
```

The agent's firewall blocks hosts other than GitHub and Copilot by default.
An admin must allow the Mezmo host, or every call is denied.

Tested 2026-10-17: the cloud agent ran this hook and blocked each tool call.
A variable and a secret set in the repository's `copilot` environment did not
reach the hook, which denied with `MEZMO_GOVERNANCE_URL must be a non-empty
string`, and with the URL set inline in the `bash` command, `MEZMO_GOVERNANCE_TOKEN
must be a non-empty string`. How to give the hook the token is not verified.
The audit log is lost when the job ends.

## MCP tools

The Copilot docs do not give the tool name format for MCP tools. The hook
sends every tool with namespace `copilot` and the name Copilot gives it, so a
policy keyed on the MCP server name does not match. This is not verified.

## Windows

The example has a `powershell` command that uses `py -3` and `$HOME`. It was
not run on Windows. VS Code Local runs it with `powershell.exe -Command`, which
exits 1 for any failed command, even the hook's own exit 2, and Local then runs
the tool. So the `preToolUse` command ends with `if ($LASTEXITCODE -ne 0) { exit 2 }`. In PowerShell, set the variables with
`$env:MEZMO_GOVERNANCE_URL = "..."` and `$env:MEZMO_GOVERNANCE_TOKEN = "..."`
before you start Copilot.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"sessionId":"smoke","timestamp":0,"cwd":".","toolName":"bash","toolArgs":"{\"command\":\"echo hi\"}"}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py preToolUse
```

It exits 0 and prints a deny. The text after `unavailable:` varies by
platform, and `hookSpecificOutput` repeats the same decision:

```json
{"permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Copilot blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>","hookSpecificOutput":{…}}
```

Then run the same line without the two inline variables, with your settings
exported. No output means Mezmo allowed the call, and Copilot goes on with its
normal permission flow. A deny prints the shape above with your policy's
reason.

## Output

Allow prints nothing. Deny, at the top level for Copilot and in
`hookSpecificOutput` for VS Code Local:

```json
{
  "permissionDecision": "deny",
  "permissionDecisionReason": "Mezmo Governance denied this exact tool call: …",
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Mezmo Governance denied this exact tool call: …"
  }
}
```

Copilot CLI 1.0.98 obeyed this deny for a `preToolUse` and a `PreToolUse` entry.

Copilot sends no tool call id. Each run uses a fresh `run-<uuid>`, so
`request_id` is `copilot:<sessionId>:run-<uuid>#<digest>`. A repeated identical
call gets its own decision. Post-tool records have `decision_id: null`.
VS Code Local sends `tool_use_id`, so there `request_id` is
`copilot:<session_id>:<tool_use_id>#<digest>`, and post-tool records carry the
decision's ids.

`toolArgs` may arrive as JSON text or as an object; the hook accepts both.
`postToolUse` and `postToolUseFailure` only append the local JSONL audit log.
A failure event, or a `toolResult.resultType` other than `success`, is
recorded as `failed`. The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

## Tests

```bash
python3 -m unittest test_hook.py -v
```
