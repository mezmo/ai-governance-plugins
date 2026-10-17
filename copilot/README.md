# GitHub Copilot + Mezmo Governance

A GitHub Copilot command hook that asks Mezmo Governance before each tool
runs. If Mezmo denies the call, or the hook cannot get an answer, the hook
prints a deny and Copilot skips the tool. After a tool finishes, the same
script appends a local JSONL record.

It works with Copilot CLI, Copilot cloud agent, and VS Code sessions that use
the Copilot harness. The VS Code Local harness uses its own payloads, which
this hook does not read.

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
Use the camelCase event names. The PascalCase names (`PreToolUse`) send
another payload, which this hook rejects, so Copilot denies every call.

## Tool names in policy

Mezmo sees every tool with namespace `copilot`. Names seen live in Copilot CLI
1.0.95: `bash` for shell commands and `view` for reads. In Autopilot, Copilot
also sends its `task_complete` tool through the hook, so a policy that denies
every tool stops Autopilot from finishing.

## Cloud agent

The cloud agent reads only `.github/hooks/*.json` in the repository. Commit
the script as `.github/hooks/mezmo_governance_hook.py`, and point the `bash`
command at it from the repository root:

```json
"bash": "python3 .github/hooks/mezmo_governance_hook.py preToolUse"
```

The agent's firewall blocks hosts other than GitHub and Copilot by default.
An admin must allow the Mezmo host, or every call is denied. The docs do not
say how a hook gets a secret such as the token; this is not verified.
The audit log is lost when the job ends.

## MCP tools

The Copilot docs do not give the tool name format for MCP tools. The hook
sends every tool with namespace `copilot` and the name Copilot gives it, so a
policy keyed on the MCP server name does not match. This is not verified.

## Windows

The example has a `powershell` command that uses `py -3` and `$HOME`. It was
not run on Windows. In PowerShell, set the variables with
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

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Copilot blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}
```

Then run the same line without the two inline variables, with your settings
exported. No output means Mezmo allowed the call, and Copilot goes on with its
normal permission flow. A deny prints the shape above with your policy's
reason.

## Output

Allow prints nothing. Deny:

```json
{ "permissionDecision": "deny", "permissionDecisionReason": "Mezmo Governance denied this exact tool call: …" }
```

Copilot sends no tool call id. Each run uses a fresh `run-<uuid>`, so
`request_id` is `copilot:<sessionId>:run-<uuid>#<digest>`. A repeated identical
call gets its own decision. Post-tool records have `decision_id: null`.

`toolArgs` may arrive as JSON text or as an object; the hook accepts both.
`postToolUse` and `postToolUseFailure` only append the local JSONL audit log.
A failure event, or a `toolResult.resultType` other than `success`, is
recorded as `failed`. The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

## Tests

```bash
python3 -m unittest test_hook.py -v
```
