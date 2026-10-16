# Codex + Mezmo Governance

A Codex CLI command hook that asks Mezmo Governance before each tool runs. If
Mezmo denies the call, or the hook cannot get an answer, the hook denies and
Codex skips the tool. After an approved tool finishes, the same script appends
a local JSONL record.

Official docs: https://developers.openai.com/codex/hooks

The script needs Python 3.9 or later and uses only the standard library.

## Fail behavior

Codex has no fail-closed setting. Codex 0.162.1 runs the tool when a hook
exits 1, for example on a crash (verified live). The example command ends
with a guard that turns any non-zero exit into exit 2, which Codex treats as
a block:

```bash
|| { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }
```

The hook exits 2 itself when it blocks on purpose, for example when a call is
still pending at its deadline. The guard keeps that exit and prints nothing
more, so the agent shows only the hook's reason. Any other non-zero exit, such
as a Python error or a missing `python3`, prints the message above. A missing
hook script makes Python exit 2 too; Python then names the file it could not
open.

With the guard, a crash blocks the call (verified live: `hook: PreToolUse
Blocked`). A wrong path or a missing `python3` also exits non-zero, so the
guard blocks those too. Codex still runs the tool when:

- the hook reaches its `timeout`;
- Codex skips the hook because it is new or changed and not yet trusted.

Keep the guard on `PreToolUse` only. Run the [smoke test](#smoke-test) after
each install, and check `/hooks` after each config change. Hosted tools such
as `WebSearch` never fire hooks.

## Install

`./mezmo-governance install codex` from the repository root does this for you, and `uninstall` undoes it. The steps below are the manual way.

1. Copy the hook:

```bash
mkdir -p ~/.codex/hooks
cp mezmo_governance_hook.py ~/.codex/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into `~/.codex/hooks.json`, or
   into `.codex/hooks.json` in a repo. The example path starts at `$HOME`, so it
   works from any directory. To keep the script elsewhere, set
   `MEZMO_GOVERNANCE_HOOK` to its absolute path. Codex runs hooks from the
   session directory, so a relative path breaks. To run the script without
   `python3` in front, run `chmod +x` on it first.

3. Export the agent key from Mezmo:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Start Codex and run `/hooks`. Review and trust the Mezmo hook entries. Codex
   skips new or changed hooks until you trust them, and warns at startup when a
   hook needs review. Repeat this after each edit to the hook entries.

Hooks are on by default. `hooks = false` under `[features]` in
`~/.codex/config.toml` turns them all off.

Codex inherits the environment of the shell that launches it.

Headless `codex exec` skips hooks that are not trusted yet, so the tool runs
ungoverned. Trust the hooks once in an interactive session first. For CI,
`codex exec --dangerously-bypass-hook-trust` runs them without stored trust.
It trusts every enabled hook, not just this one, so use it only where you
control every hook source.

To gate only some tools, change `matcher`. Examples: `Bash`, `^apply_patch$`,
`mcp__kubernetes__.*`. `apply_patch` also matches `Edit` and `Write`.

## Tool names in policy

Mezmo sees the Codex tool name with namespace `codex`. Names seen live in
Codex 0.162.1: `Bash` for shell commands and `apply_patch` for file edits. An
MCP tool arrives with its server as the namespace. Codex has no read tool: it
reads files with `Bash` (`cat`), so a policy that denies `Bash` also blocks reads.

## Windows

The example command uses POSIX shell syntax (`${VAR:-default}`, `$HOME`) and
`python3`. On Windows, add `commandWindows` to each handler with an absolute
path. For `PreToolUse`, add the guard in PowerShell form:

```json
"commandWindows": "py -3 C:\\Users\\<you>\\.codex\\hooks\\mezmo_governance_hook.py; if ($LASTEXITCODE -ne 0) { exit 2 }"
```

The Codex docs do not say which shell runs `commandWindows`. The guard
assumes PowerShell; this is not verified.

In PowerShell, set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_TOKEN = "..."` before you start Codex.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_use_id":"smoke-1","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Codex blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}}
```

Then check your key with the variables from step 3 set. This asks Mezmo about
one `echo hi` call:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_use_id":"smoke-2","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | python3 mezmo_governance_hook.py
```

No output means Mezmo allowed the call. A deny prints the shape above with your
policy's reason. Use a new `tool_use_id` for each run: Mezmo returns the
earlier decision for an id it has seen.

## What the hook sends

`tool_name` `mcp__kubernetes__pods_delete` becomes namespace `kubernetes` and
name `pods_delete`. Built-in tools (`Bash`, `apply_patch`, `update_plan`) use
namespace `codex`.

`request_id` is `codex:<session_id>:<turn_id>:<tool_use_id>#<digest>` when
`turn_id` is present. `<digest>` hashes the tool and its arguments.
`decision_id` is uuid5 of that string, so a retry of the same call polls the
existing decision instead of opening a second one, and an edited call gets its
own ([identity](../docs/protocol.md#identity)).

PreToolUse deny stdout:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Mezmo Governance denied this exact tool call: …"
  }
}
```

If Mezmo is down, JSON is malformed, or the deadline expires while the decision
is still pending, the hook denies. Exit code 2 is used only for broken stdin or
an unsupported event; Codex treats it as a block.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-codex`.
