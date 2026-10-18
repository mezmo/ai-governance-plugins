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

Tested version: Windsurf 1.126.0 (Devin Desktop 3.10.67), run live on
2026-10-17 against `tools/mock_governance.py` and production Mezmo. Cascade's
`touch marker` was denied and no file was written; a file read was approved.
With the guard, a hook crash blocked the command. The other hook fields, exit
codes and file locations below come from those docs and from the Devin Desktop
3.10.67 build for macOS on Apple silicon (`devin-desktop --version` prints
1.126.0, commit `0b186d6e13a1a48a46430c51fe414dd0747d46f6`; zip SHA-256
`8063233fcc937a9fdb53191ff25de5313b0a580f1b0eb6be6f660a154f40b817`, which
matches the Windsurf update server).

## Partial coverage

Cascade has hooks only for terminal commands, file reads, file edits and MCP
calls. These actions get no hook, so Mezmo never sees them and they run
ungoverned:

- grep and find searches;
- new files written whole (`write_to_file`);
- web search and URL reads;
- git commits;
- web app deploys;
- subagent tasks.

Terminal reads, browser DOM reads, clipboard use and custom tools get no hook
either. If your policy must cover any of these, Cascade cannot enforce it
today.

Windsurf loads no hooks at all while a workspace is open in Restricted Mode,
the user-level hook included. Trust each workspace you use with Cascade.

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
you, and `uninstall` undoes it. The steps below are the manual way. Run them
from the repository root. Windsurf has no override for the `~/.codeium`
folder.

1. Copy the hook:

```bash
mkdir -p ~/.codeium/windsurf/hooks
cp windsurf/mezmo_governance_hook.py ~/.codeium/windsurf/hooks/
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

3. Export the URL and the service account token. The token is a standard
   Mezmo service key, not a key of the agent; see
   [Connect to Mezmo](../README.md#connect-to-mezmo):

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Quit and start Windsurf, and trust the workspace. Ask Cascade to run
   `ls`. Mezmo, or the mock from [Verify](#verify), shows a
   `windsurf:run_command` call.

The hook reads these variables from the environment Windsurf gives it.
VS Code-based editors read your login shell's environment at start, so set
them in your shell profile, then quit and start Windsurf. Started from the Dock on
2026-10-17, Windsurf did not pass them to the hook, which blocked the command with
`MEZMO_GOVERNANCE_URL must be a non-empty string`. Started from a shell that
exports them (`devin-desktop <folder>`), the hook got them. For a Dock launch, set them
for the login session and restart Windsurf; verified 2026-10-17:

```bash
launchctl setenv MEZMO_GOVERNANCE_URL "$MEZMO_GOVERNANCE_URL"
launchctl setenv MEZMO_GOVERNANCE_TOKEN "$MEZMO_GOVERNANCE_TOKEN"
```

`launchctl setenv` lasts until logout, and any app you start can read the token. Without them the hook
denies every call and names the missing variable.

## Verify

From the repository root, with the two variables exported:

```bash
./mezmo-governance doctor windsurf
./mezmo-governance smoke windsurf
```

`doctor` checks the install, the settings and the key. `smoke` runs the
installed hook against its own mock: one shell call must be denied and one
read approved. Neither starts the agent.

To check that the agent calls the hook, run
`./mezmo-governance mock --deny run_command` in a second terminal. In the agent's
terminal, export the two lines it prints, start the agent, and ask it to run
`ls`. The
mock prints a `denied` line for `windsurf:run_command`. No line means the hook did not
run. Windsurf has no headless mode, so type the request in Cascade.

## Smoke test

Run these from the `windsurf/` folder. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"agent_action_name":"pre_run_command","trajectory_id":"smoke","tool_info":{"command_line":"echo hi","cwd":"."}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py; echo "exit $?"
```

It exits 2, prints the reason on stderr, and prints the same reason as JSON
on stdout, which Windsurf ignores. The text after `unavailable:`
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

## What the hook covers

The hook gates these four Cascade hooks, one for each kind of action:

| Hook | Cascade steps it covers |
|---|---|
| `pre_run_command` | terminal commands |
| `pre_read_code` | file views, code item views, directory lists, notebook reads, code context search |
| `pre_write_code` | code edits, proposed code, notebook edits |
| `pre_mcp_tool_use` | MCP tool calls, MCP resource lists and reads |

The step lists, and the steps with no hook in [Partial coverage](#partial-coverage),
come from the 1.126.0 language server. The Restricted Mode rule is in the docs,
and the language server logs
`[HOOKS] Skipping all hook files: workspace is untrusted`. Whether Cascade can
still run tools in such a workspace is not verified.

## Windows

Windows is not supported. On Windows, Windsurf runs `command` with
`powershell -Command`. PowerShell cannot parse the bash `|| exit 2` guard and
exits 1, so the action runs: the hook would fail open. `./mezmo-governance install windsurf` refuses on
Windows, and changes nothing.

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

## Troubleshooting

Run `./mezmo-governance doctor --probe windsurf` from the repository root.
[Troubleshooting](../README.md#troubleshooting) in the root README lists each
error and its fix. For Windsurf, trust the workspace: Restricted Mode loads no hook. A Dock launch passes no variables unless you ran `launchctl setenv`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-windsurf`.
