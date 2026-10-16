# Gemini CLI + Mezmo Governance

A Gemini CLI command hook that asks Mezmo Governance before each tool runs. If
Mezmo denies the call, or the hook cannot get an answer, the hook prints a
deny and Gemini CLI skips the tool. After a tool finishes, the same script
appends a local JSONL record.

Official docs: https://geminicli.com/docs/hooks/reference/

The script needs Python 3.9 or later and uses only the standard library.

Tested version: Gemini CLI 0.64.0, run live against
`tools/mock_governance.py`.

## Fail behavior

Gemini CLI has no fail-closed setting. Gemini CLI 0.64.0 runs the tool when a
hook exits 1, for example on a crash (verified live). The example
`BeforeTool` command ends with a guard that turns any non-zero exit into exit
2, which Gemini CLI treats as a block:

```bash
|| { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }
```

The hook exits 2 itself when it blocks on purpose, for example when a call is
still pending at its deadline. The guard keeps that exit and prints nothing
more, so the agent shows only the hook's reason. Any other non-zero exit, such
as a Python error or a missing `python3`, prints the message above. A missing
hook script makes Python exit 2 too; Python then names the file it could not
open.

The docs say exit 2 blocks. The guard itself was verified live in Codex, not
in Gemini CLI. With it, a crash, a wrong path or a missing `python3` blocks
the call. Gemini CLI still runs the tool when:

- the hook reaches its `timeout` (from the source code, not verified);
- the hook is disabled in `/hooks`, or a changed project hook is not trusted.

Keep the guard on `BeforeTool` only. Run the [smoke test](#smoke-test) after
each install.

## Install

`./mezmo-governance install gemini-cli` from the repository root installs
this folder as a Gemini CLI extension:

```bash
gemini extensions install ./gemini-cli --consent --skip-settings
```

Gemini CLI asks you to trust the folder, copies it to
`~/.gemini/extensions/mezmo-governance/`, and loads the extension's
`hooks/hooks.json`, the same two hooks as `settings.example.json`.
`gemini extensions uninstall mezmo-governance` removes it. Without the
extension:

1. Copy the hook:

```bash
mkdir -p ~/.gemini/hooks
cp mezmo_governance_hook.py ~/.gemini/hooks/
```

2. Merge [settings.example.json](settings.example.json) into
   `~/.gemini/settings.json`. Prefer this user-level file: Gemini CLI loads
   project `.gemini/settings.json` hooks only in a trusted folder.
   The example path starts at `$HOME`, so it works from any directory. To keep
   the script elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path.

3. Export the settings in the shell that starts `gemini`:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Run `/hooks panel` and check that both hooks are listed and enabled. Gemini
   CLI warns about a project hook whose name or command changed until you
   trust it again.

Headless `gemini -p` refuses to run in an untrusted folder: "Gemini CLI is
not running in a trusted directory". For CI, set
`GEMINI_CLI_TRUST_WORKSPACE=true` or pass `--skip-trust`. Both trust the
whole workspace for that run, including its project hooks and settings.

The `BeforeTool` timeout is in milliseconds. The example sets `600000`, so the
script's own 590 second deadline ends first and returns a clear deny.

If you turn on `security.environmentVariableRedaction`, Gemini CLI removes
variables whose names contain `KEY` or `TOKEN` from the hook. Add
`MEZMO_GOVERNANCE_TOKEN` to its `allowed` list, or every call is denied.

## Tool names in policy

Mezmo sees built-in tools with namespace `gemini_cli`. Names seen live in
Gemini CLI 0.64.0: `run_shell_command` for shell commands and `read_file` for
reads.

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
"command": "py -3 C:/Users/<you>/.gemini/hooks/mezmo_governance_hook.py; if ($LASTEXITCODE -ne 0) { exit 2 }"
```

The `if ($LASTEXITCODE ...)` part is the crash guard in PowerShell form, for
`BeforeTool` only. It was not run on Windows.

Set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_TOKEN = "..."` before you start Gemini CLI.

## Smoke test

Run these from the directory that holds the script. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"BeforeTool","session_id":"smoke","tool_name":"run_shell_command","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py
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
`request_id` is `gemini_cli:<session_id>:run-<uuid>#<digest>`. A repeated
identical call gets its own decision. Post-tool records have `decision_id: null`.

`AfterTool` events only append the local JSONL audit log. A `tool_response`
with an `error` field is recorded as `failed`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
