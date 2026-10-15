# Codex + Mezmo Governance

A Codex CLI command hook that asks Mezmo Governance before each tool runs. Denied
or failed checks stop the call. After an approved tool finishes, the same
script appends a local JSONL record.

Requires Codex with hooks enabled. Official docs:
https://developers.openai.com/codex/hooks

The script needs Python 3.9 or later and uses only the standard library.

## Install

1. Copy the hook next to Codex config:

```bash
mkdir -p ~/.codex/hooks
cp mezmo_governance_hook.py ~/.codex/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into `~/.codex/hooks.json`, or
   into `.codex/hooks.json` in a repo. Paths in the example assume the user copy
   above. Point `MEZMO_GOVERNANCE_HOOK` at another path if you keep the script
   in this checkout.

3. Turn hooks on in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

4. Export the agent key from Mezmo:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

Codex inherits the environment of the shell that launches it. `--full-auto`
still runs hooks.

To gate only some tools, change `matcher`. Examples: `Bash`, `^apply_patch$`,
`mcp__kubernetes__.*`. `apply_patch` also matches `Edit` and `Write`. Hosted
tools such as `WebSearch` do not fire these hooks.

## What the hook sends

`tool_name` `mcp__kubernetes__pods_delete` becomes namespace `kubernetes` and
name `pods_delete`. Built-in tools (`Bash`, `apply_patch`, `update_plan`) use
namespace `codex`.

`request_id` is `codex:<session_id>:<turn_id>:<tool_use_id>` when `turn_id` is
present. `decision_id` is uuid5 of that string, so a retry of the same call polls
the existing decision instead of opening a second one.

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
is still pending, the hook denies. Exit code 2 is used only for broken stdin,
which Codex also treats as a block.

## Tests

```bash
python3 -m unittest test_hook.py -v
```

From the repo root: `make test-codex`.
