# Claude Code + Mezmo Governance

A Claude Code hook that asks Mezmo Governance before each tool runs. The same
script can be installed as a project/user hook or as a Claude Code plugin.

Official docs: https://code.claude.com/docs/en/hooks

The script needs Python 3.9 or later and uses only the standard library.

Use Claude Code v2.1.295 or later. The examples set `"onFailure": "block"` on
`PreToolUse`, so a missing script, a crash or a timeout blocks the tool call.
Older versions do not support `onFailure`: there, such a failure lets the tool
run.

## Install as settings

1. Copy the hook:

```bash
mkdir -p ~/.claude/hooks
cp mezmo_governance_hook.py ~/.claude/hooks/
```

2. Merge [settings.example.json](settings.example.json) into
   `~/.claude/settings.json` or `.claude/settings.json`. The example path starts
   at `$HOME`, so it works from any directory. To keep the script elsewhere,
   set `MEZMO_GOVERNANCE_HOOK` to its absolute path. To run the script without
   `python3` in front, run `chmod +x` on it first.

3. Export:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

Claude Code also accepts `if` matchers such as `Bash(git *)` or
`mcp__kubernetes__.*`. Plugin-bundled MCP tools use a scoped name
`mcp__plugin_<plugin>_<server>__<tool>`.

## Install as a plugin

This directory is a plugin (`.claude-plugin/plugin.json` plus `hooks/hooks.json`).
Enable it from a local marketplace or by pointing Claude Code at this folder.
Plugin hooks run `python3 "${CLAUDE_PLUGIN_ROOT}/mezmo_governance_hook.py"`.

`./mezmo-governance install claude-code` from the repository root runs
`claude plugin install mezmo-governance --marketplace <repository> -s user`.
That install asks for nothing: run `/plugin configure mezmo-governance@mezmo`
in Claude Code to set the URL and key, or export the variables.
`./mezmo-governance uninstall claude-code` uninstalls the plugin and removes
the `mezmo` marketplace if it points at this checkout.

When you enable the plugin, Claude Code asks for the Mezmo Governance URL
(default `https://api.mezmo.com/governance/workflow/authorize`) and the agent
access key. It stores the key in the system credential store, not in
`settings.json`, and passes both to the hook as `CLAUDE_PLUGIN_OPTION_*`
variables. Change the URL later with `/config`; the key, being sensitive, does
not appear there. `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_ACCESS_KEY`, when set, win over the plugin options.

## Windows

Claude Code runs the command with Git Bash when it is installed, so the
example works there if `python3` is on `PATH`. Without Git Bash it uses
PowerShell, which does not expand `${VAR:-default}`. With `onFailure: "block"`,
that blocks every tool call. Then replace each command with an absolute path:

```json
"command": "py -3 C:/Users/<you>/.claude/hooks/mezmo_governance_hook.py"
```

In PowerShell, set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_ACCESS_KEY = "..."` before you start Claude Code.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_use_id":"smoke-1","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_ACCESS_KEY="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Claude Code blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}}
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

MCP names `mcp__<server>__<tool>` split into that server and tool. Built-in tools
(`Bash`, `Edit`, `Write`, `Read`) use namespace `claude_code`.

`request_id` is `claude_code:<session_id>:<tool_use_id>#<digest>`, or
`claude_code:<session_id>:<turn_id>:<tool_use_id>#<digest>` when `turn_id` is
present. `<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)).

PreToolUse deny uses `hookSpecificOutput.permissionDecision: "deny"`. Exit code
2 also blocks. PostToolUse and PostToolUseFailure only append
`~/.mezmo/governance-audit.jsonl` (or `MEZMO_GOVERNANCE_AUDIT_LOG`) and never
block.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
