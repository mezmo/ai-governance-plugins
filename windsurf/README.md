# Windsurf + Mezmo Governance

A Windsurf Cascade hook that asks Mezmo Governance before Cascade runs a
command, reads or writes a file, or calls an MCP tool. If Mezmo denies the
call, or the hook cannot get an answer, the hook exits 2 and Cascade skips the
action. After each of those actions, the same script appends a local JSONL
record.
The audit log keeps up to 2000 characters of each MCP tool result, which can
hold secrets; the hook creates it readable by you alone (file 0600, folder
0700).

Windsurf is now called Devin Desktop; its Cascade agent and hook format are the
same. Official docs: https://docs.devin.ai/desktop/cascade/hooks
(docs.windsurf.com/windsurf/cascade/hooks redirects there), read 2026-10-17.

The script needs Python 3.9 or later and uses only the standard library.

Not run in Windsurf. The hook fields, exit codes and file locations below come
from those docs and from the Devin Desktop 3.10.67 build for macOS on Apple
silicon (`devin-desktop --version` prints 1.126.0, commit
`0b186d6e13a1a48a46430c51fe414dd0747d46f6`; zip SHA-256
`8063233fcc937a9fdb53191ff25de5313b0a580f1b0eb6be6f660a154f40b817`, which
matches the Windsurf update server). Its language server was read, never
started. The hook script itself passes its tests and
`./mezmo-governance smoke`.

## What Cascade does not hook

Cascade has hooks for four kinds of action only. The hook governs these:

| Hook | Cascade steps it covers |
|---|---|
| `pre_run_command` | terminal commands |
| `pre_read_code` | file views, code item views, directory lists, notebook reads, code context search |
| `pre_write_code` | code edits, proposed code, notebook edits |
| `pre_mcp_tool_use` | MCP tool calls, MCP resource lists and reads |

No hook runs for any other Cascade step, so Mezmo never sees them and they run
ungoverned. In the 1.126.0 language server these include grep and find
searches, new files written whole (`write_to_file`), web search and URL reads,
terminal reads, git commits, web app deploys, browser DOM reads, clipboard
use, subagent tasks and custom tools. If your policy must cover those, Cascade
cannot enforce it today.

Hooks do not load at all while a workspace is open in Restricted Mode: the
user-level hook is skipped with the rest (docs, and the language server logs
`[HOOKS] Skipping all hook files: workspace is untrusted`). Whether Cascade
can still run tools in such a workspace is not verified. Trust each workspace
you use with Cascade.

## Fail behavior

Windsurf has no fail-closed setting. A pre hook blocks only when it exits 2;
any other exit lets the action run (docs, Exit Codes). In the 1.126.0
language server a hook that cannot start, or that a signal kills, also lets the
action run. The example command ends with a guard that turns any non-zero exit
into exit 2:

```bash
|| { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }
```

The hook exits 2 itself when it blocks on purpose, for example when a call is
still pending at its deadline. The guard keeps that exit and prints nothing
more, so Cascade shows only the hook's reason. Any other non-zero exit, such
as a Python error, a missing `python3` or a killed Python, prints the message
above. A missing hook script makes Python exit 2 too; Python then names the
file it could not open. Only `bash` itself failing to start fails open.

Windsurf reads no hook stdout. It joins the stderr of each hook that exits 2
into the error Cascade sees. The script prints its reason there.

Windsurf has no hook timeout setting, and the 1.126.0 language server sets no
deadline of its own on a hook (read from the binary, not verified live). The
script's own 590 second deadline ends first and returns a clear deny; lower it
with `MEZMO_GOVERNANCE_TIMEOUT_SECONDS`.

Keep the guard on the `pre_*` hooks only. Run the [smoke test](#smoke-test)
after each install.

## Install

`./mezmo-governance install windsurf` from the repository root does this for
you, and `uninstall` undoes it. The steps below are the manual way.

1. Copy the hook:

```bash
mkdir -p ~/.codeium/windsurf/hooks
cp mezmo_governance_hook.py ~/.codeium/windsurf/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into
   `~/.codeium/windsurf/hooks.json`. The example path starts at `$HOME`, so it
   works from any workspace. To keep the script elsewhere, set
   `MEZMO_GOVERNANCE_HOOK` to its absolute path.

   Windsurf drops the whole file, and logs
   `[HOOKS] Failed to load hooks from user location`, when it has a top-level
   key other than `hooks`, an event name it does not know, or an entry without
   a `command` or `powershell` string. Every hook in it then stops running.
   `install` refuses, and changes nothing, while the file has one of these.

   Windsurf also runs the system-level and workspace hooks
   (`.devin/hooks.json`, or the older `.windsurf/hooks.json`): the files are
   merged, and none hides another. Windsurf Next and Insiders read
   `~/.codeium/windsurf-next/` and `~/.codeium/windsurf-insiders/`, and the
   JetBrains plugin reads `~/.codeium/hooks.json`; `install` covers only the
   stable editor.

3. Export the agent key from Mezmo:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Quit and start Windsurf. Run a command in Cascade and check that Mezmo
   records the call.

The hook reads these variables from the environment Windsurf gives it.
VS Code-based editors read your login shell's environment at start, so set
them in your shell profile, then quit and start Windsurf; this is not verified
for Windsurf. Without them the hook denies every call and names the missing
variable.

### Windows

Windsurf runs `command` with `powershell -Command` on Windows when the entry
has no `powershell` field. PowerShell cannot parse the bash guard, exits 1,
and the action runs. On Windows, add a `powershell` field to each `pre_*`
entry, with an absolute path and the guard in PowerShell form:

```json
"powershell": "py -3 C:\\Users\\<you>\\.codeium\\windsurf\\hooks\\mezmo_governance_hook.py; if ($LASTEXITCODE -ne 0) { exit 2 }"
```

Not run on Windows. In PowerShell, set the variables with
`$env:MEZMO_GOVERNANCE_URL = "..."` and `$env:MEZMO_GOVERNANCE_TOKEN = "..."`.

## Tool names in policy

Mezmo sees each built-in action with namespace `windsurf`:

| Hook | Tool name | Arguments |
|---|---|---|
| `pre_run_command` | `run_command` | `command_line`, `cwd` |
| `pre_read_code` | `read_code` | `file_path`, which may be a directory |
| `pre_write_code` | `write_code` | `file_path`, `edits` (each `old_string`, `new_string`) |
| `pre_mcp_tool_use` | the MCP tool name | the MCP tool's own arguments |

An MCP tool arrives with its server as the namespace. The hook denies a call
from an MCP server named `windsurf`: its tools would pass as built-ins.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"agent_action_name":"pre_run_command","trajectory_id":"smoke","tool_info":{"command_line":"echo hi","cwd":"."}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py; echo "exit $?"
```

It exits 2 and prints the reason on stderr. The text after `unavailable:`
varies by platform:

```text
Mezmo Governance could not authorize this exact tool call, so Windsurf blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>
```

Then check your key with the variables from step 3 set. This asks Mezmo about
one `echo hi` call:

```bash
echo '{"agent_action_name":"pre_run_command","trajectory_id":"smoke","tool_info":{"command_line":"echo hi","cwd":"."}}' \
  | python3 mezmo_governance_hook.py; echo "exit $?"
```

Exit 0 with no output means Mezmo allowed the call. Exit 2 prints your
policy's reason.

## What the hook sends

Windsurf sends no tool call id, so `request_id` is
`windsurf:<trajectory_id>:run-<uuid>#<digest>`, new for every run. The
trajectory is the Cascade conversation. A repeated identical call opens a new
decision instead of reusing an earlier one through HTTP 409
([identity](../docs/protocol.md#identity)). The post-action record has
`decision_id` and `request_id` set to `null` for the same reason.

On a deny the hook exits 2, prints the reason on stderr, and prints
`{"decision":"deny","reason":"…"}` on stdout, which Windsurf ignores.

If Mezmo is down, JSON is malformed, a field the call needs is missing, or the
deadline expires while the decision is still pending, the hook denies. It
denies on input it cannot read too, such as broken or too deeply nested JSON,
or an unsupported event.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-windsurf`.
