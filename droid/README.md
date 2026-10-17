# Factory Droid + Mezmo Governance

A Factory Droid command hook that asks Mezmo Governance before each tool runs.
If Mezmo denies the call, or the hook cannot get an answer, the hook denies and
Droid skips the tool. After a tool finishes, the same script appends a local
JSONL record.
The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

Official docs: https://docs.factory.com/reference/hooks-reference (hooks) and
https://docs.factory.com/harness/plugins (plugins), read 2026-10-17.

The script needs Python 3.9 or later and uses only the standard library.

Not run in Droid. The hook fields, exit codes and file locations below come
from those docs and from the Droid 0.244.0 binary (`@factory/cli-darwin-arm64`
on npm), read but never started. The hook script itself passes its tests and
`./mezmo-governance smoke`.

## Fail behavior

Droid has no fail-closed setting. In Droid 0.244.0 a hook that exits 1,
crashes, cannot start or is killed only logs a warning, and the tool runs.
The example command ends with a guard that turns any non-zero exit into exit
2, which Droid treats as a block:

```bash
|| { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }
```

The hook exits 2 itself when it blocks on purpose, for example when a call is
still pending at its deadline. The guard keeps that exit and prints nothing
more, so the agent shows only the hook's reason. Any other non-zero exit, such
as a Python error or a missing `python3`, prints the message above. A missing
hook script makes Python exit 2 too; Python then names the file it could not
open.

Droid reads the JSON deny first, then the exit code. Droid still runs the tool
when:

- the hook reaches its `timeout` (Droid 0.244.0 binary, not verified live);
- hooks are off: `hooksDisabled: true` in `hooks.json` or under `hooks` in
  `settings.json`;
- org-managed settings set `allowManagedHooksOnly`, which ignores user hooks.

Droid's `timeout` is in seconds and defaults to 60. The example sets the
`PreToolUse` `timeout` to `600`, so the script's own 590 second deadline ends
first and returns a clear deny. Keep the `timeout` at least 10 seconds above
`MEZMO_GOVERNANCE_TIMEOUT_SECONDS`.

In an interactive session in a folder you have not trusted yet, Droid 0.244.0
skips every hook until you trust the folder. Whether a tool can run before
that is not verified.

Keep the guard on `PreToolUse` only. Run the [smoke test](#smoke-test) after
each install, and check `/hooks` after each config change: Droid reads hooks
at startup.

## Install

`./mezmo-governance install droid` from the repository root does this for you,
and `uninstall` undoes it. The steps below are the manual way.

1. Copy the hook:

```bash
mkdir -p ~/.factory/hooks
cp mezmo_governance_hook.py ~/.factory/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into `~/.factory/hooks.json`.
   That file holds the events at its top level, with no `hooks` key. The
   example path starts at `$HOME`, so it works from any directory. To keep the
   script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path.
   `FACTORY_HOME_OVERRIDE` moves `~/.factory` to `$FACTORY_HOME_OVERRIDE/.factory`.

   Droid lays `hooks.json` over the `hooks` key of `settings.json`, one event
   at a time (0.244.0; its docs say `settings.json` hooks count only while
   `hooks.json` is absent). It reads the older `~/.factory/hooks/hooks.json`
   only while `hooks.json` is absent. Move hooks from either into `hooks.json`
   first, or they stop running. `install` refuses, and changes nothing, until
   you do.

3. Export the agent key from Mezmo:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Start Droid and run `/hooks`. Check that the **Effective** tab lists both
   Mezmo hooks.

Droid inherits the environment of the shell that launches it.

To gate only some tools, set `matcher`: an exact name such as `Execute`, or a
case-sensitive regex such as `mcp__.*`. An empty, omitted or `*` matcher
matches every tool.

### As a Droid plugin

This repository is a Droid marketplace: `.factory-plugin/marketplace.json`
lists the plugin in this folder. Droid reads that file before
`.claude-plugin/marketplace.json`, so it installs this hook, not the Claude
Code plugin.

```bash
droid plugin marketplace add mezmo/ai-governance-plugins
droid plugin marketplace list
droid plugin install mezmo-governance@<marketplace> --scope user
```

Droid names the marketplace after its source; `droid plugin marketplace list`
shows the name. The plugin's [hooks/hooks.json](hooks/hooks.json) is the
example with `${DROID_PLUGIN_ROOT}` for the script path. Use the plugin or the
file install, not both: both run the hook twice. `doctor` and `smoke` check
only the file install. Not run.

Before this folder existed, the same commands offered the Claude Code plugin
`mezmo`. In Droid it sends the Claude Code origin, and Droid 0.244.0 drops its
`onFailure: block`, so a hook crash lets the tool run. If you installed it, run
`droid plugin uninstall mezmo@<marketplace> --scope user`.

## Tool names in policy

Mezmo sees the Droid tool name with namespace `droid`. Names from the Droid
docs: `Execute` for shell commands, `Read`, `Edit`, `Create`, `ApplyPatch`,
`LS`, `Glob`, `Grep`, `Task`, `FetchUrl` and `WebSearch`. An MCP tool arrives
with its server as the namespace.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_name":"Execute","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Droid blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}}
```

Then check your key with the variables from step 3 set. This asks Mezmo about
one `echo hi` call:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_name":"Execute","tool_input":{"command":"echo hi"}}' \
  | python3 mezmo_governance_hook.py
```

No output means Mezmo allowed the call. A deny prints the shape above with your
policy's reason.

## What the hook sends

`tool_name` `mcp__kubernetes__pods_delete` becomes namespace `kubernetes` and
name `pods_delete`. Built-in tools use namespace `droid`. The hook denies a
call from an MCP server named `droid`: its tools would pass as built-ins.

Droid sends no tool call id, so `request_id` is
`droid:<session_id>:run-<uuid>#<digest>`, new for every run. A repeated
identical call opens a new decision instead of reusing an earlier one through
HTTP 409 ([identity](../docs/protocol.md#identity)). The post-tool record has
`decision_id` and `request_id` set to `null` for the same reason.

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
is still pending, the hook denies. On input it cannot read, such as broken or
too deeply nested JSON, or an unsupported event, it prints a deny and exits 2;
Droid treats exit 2 as a block.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-droid`.
