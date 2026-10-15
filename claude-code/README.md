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
   `~/.claude/settings.json` or `.claude/settings.json`. Set
   `MEZMO_GOVERNANCE_HOOK` if the script lives somewhere else.

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

The environment variables still have to be present in the Claude Code process.

## What the hook sends

MCP names `mcp__<server>__<tool>` split into that server and tool. Built-in tools
(`Bash`, `Edit`, `Write`, `Read`) use namespace `claude_code`.

`request_id` is `claude_code:<session_id>:<tool_use_id>`, or
`claude_code:<session_id>:<turn_id>:<tool_use_id>` when `turn_id` is present.

PreToolUse deny uses `hookSpecificOutput.permissionDecision: "deny"`. Exit code
2 also blocks. PostToolUse and PostToolUseFailure only append
`~/.mezmo/governance-audit.jsonl` (or `MEZMO_GOVERNANCE_AUDIT_LOG`) and never
block.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
