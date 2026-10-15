# Cursor + Mezmo Governance

A Cursor agent hook that asks Mezmo Governance before a tool runs. The example
config uses `preToolUse` so every tool type is gated once (`Shell`, `Read`,
`Write`, MCP, Task). Do not also register `beforeShellExecution` /
`beforeMCPExecution` on the same script unless you want a second authorize call
for those tools.

Set `failClosed: true` on the gating hook. Cursor allows the action through when
a hook crashes unless that flag is on.

Official docs: https://cursor.com/docs/hooks

The script needs Python 3.9 or later and uses only the standard library.

## Install

```bash
mkdir -p ~/.cursor/hooks
cp mezmo_governance_hook.py ~/.cursor/hooks/
```

Merge [hooks.example.json](hooks.example.json) into `~/.cursor/hooks.json` or
`.cursor/hooks.json`. Then:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

Optional matchers: `Shell`, `Read`, `Write`, `MCP:<tool_name>`. Cloud agents
load project `.cursor/hooks.json` only, not `~/.cursor/hooks.json`. MCP
lifecycle hooks are deferred in cloud agents; `preToolUse` is the safer gate.

If you prefer the MCP-specific events, the script also handles
`beforeMCPExecution` / `afterMCPExecution` (`tool_input` may be a JSON string,
`mcp_server_name` becomes `tool_namespace`) and `beforeShellExecution` /
`afterShellExecution` (tool name `Shell`, arguments `{ "command": "..." }`).

## Output

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

`request_id` is `cursor:<conversation_id>:<tool_use_id>`. Post-tool events only
append the local JSONL audit log.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
