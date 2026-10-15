# Gemini CLI + Mezmo Governance

A Gemini CLI command hook that asks Mezmo Governance before each tool runs. If
Mezmo denies the call, or the hook cannot get an answer, the hook prints a
deny and Gemini CLI skips the tool. After a tool finishes, the same script
appends a local JSONL record.

Official docs: https://geminicli.com/docs/hooks/reference/

The script needs Python 3.9 or later and uses only the standard library.

Tested version: none. Gemini CLI was not installed when this adapter was
written. The tests run the script against the input and output the docs give.

## Fail-open limit

Gemini CLI has no fail-closed setting. The hook denies whenever it runs and
gets no clear approval, but Gemini CLI still runs the tool when:

- the script crashes: Python exits 1, which Gemini CLI treats as a warning;
- the hook reaches its `timeout` (from the source code, not verified);
- the hook is disabled in `/hooks`, or a changed project hook is not trusted.

Gemini CLI does not document what happens when the command cannot start, for
example a wrong path or no `python3`. Run the [smoke test](#smoke-test) after
each install.

## Install

1. Copy the hook:

```bash
mkdir -p ~/.gemini/hooks
cp mezmo_governance_hook.py ~/.gemini/hooks/
```

2. Merge [settings.example.json](settings.example.json) into
   `~/.gemini/settings.json`, or into `.gemini/settings.json` in a project.
   The example path starts at `$HOME`, so it works from any directory. To keep
   the script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path.

3. Export the settings in the shell that starts `gemini`:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

4. Run `/hooks panel` and check that both hooks are listed and enabled. Gemini
   CLI warns about a project hook whose name or command changed until you
   trust it again.

The `BeforeTool` timeout is in milliseconds. The example sets `600000`, so the
script's own 590 second deadline ends first and returns a clear deny.

If you turn on `security.environmentVariableRedaction`, Gemini CLI removes
variables whose names contain `KEY` or `TOKEN` from the hook. Add
`MEZMO_GOVERNANCE_ACCESS_KEY` to its `allowed` list, or every call is denied.

## MCP tools

Gemini CLI names an MCP tool `mcp_<server>_<tool>`. Server names can contain
`_`, so the name alone does not say where the server name ends. The hook reads
the namespace and tool name from `mcp_context.server_name` and
`mcp_context.tool_name`. These fields are in the Gemini CLI source, not in the
docs: this is not verified. Without `mcp_context`, an MCP call goes to Mezmo
with namespace `gemini_cli` and the full `mcp_...` name.

## Windows

Gemini CLI runs hook commands through PowerShell on Windows (from the source
code, not verified). The example uses POSIX shell syntax. On Windows, use an
absolute path:

```json
"command": "py -3 C:/Users/<you>/.gemini/hooks/mezmo_governance_hook.py"
```

Set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_ACCESS_KEY = "..."` before you start Gemini CLI.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"BeforeTool","session_id":"smoke","tool_name":"run_shell_command","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_ACCESS_KEY="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"decision":"deny","reason":"Mezmo Governance could not authorize this exact tool call, so Gemini CLI blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}
```

Then run the same line without the two inline variables, with your settings
exported. No output means Mezmo allowed the call. A deny prints the shape
above with your policy's reason.

## Output

Allow prints nothing. Deny:

```json
{ "decision": "deny", "reason": "Mezmo Governance denied this exact tool call: …" }
```

Gemini CLI sends no tool call id. Each run uses a fresh `run-<uuid>`, so
`request_id` is `gemini_cli:<session_id>:run-<uuid>`. A repeated identical
call gets its own decision. Post-tool records have `decision_id: null`.

`AfterTool` events only append the local JSONL audit log. A `tool_response`
with an `error` field is recorded as `failed`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
