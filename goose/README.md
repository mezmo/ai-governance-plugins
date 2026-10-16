# Goose + Mezmo Governance

A Goose plugin with a command hook that asks Mezmo Governance before each tool
runs. If Mezmo denies the call, or the hook cannot get an answer, the hook
prints a block decision and Goose skips the tool. After a tool finishes, the
same script appends a local JSONL record.

Official docs: https://goose-docs.ai/docs/guides/context-engineering/hooks/

The script needs Python 3.9 or later and uses only the standard library.

Tested version: Goose CLI 1.54.0 (Homebrew formula `block-goose-cli`), run
live with `goose run` and OpenAI `gpt-5-mini` against
`tools/mock_governance.py`. Do not use `brew install goose`: that formula is a
different program, a database migration tool.

## Fail behavior

The `PreToolUse` action sets `"on_failure": "block"`. Goose then blocks the
tool call when the hook crashes, cannot start, times out, prints output Goose
cannot read, or does not receive its payload. Verified live in 1.54.0:

- a crash (exit 1) blocked the call: "Tool call blocked because policy hook
  `mezmo-governance` could not complete: the hook exited with status 1 and no
  usable decision";
- a hook that ran past a 3 second `timeout` blocked the call: "the hook
  command failed to run";
- the same crash without `on_failure` let `ls` run. That is Goose's default.

A Goose version without `on_failure` ignores the field, so a crash there lets
the tool run. Goose still runs the tool when:

- the plugin is turned off: listed in `disabledPlugins` in a Goose
  `settings.json`, or set to `enabled: false` under `plugins` in Goose's
  `config.yaml`;
- `hooks.json` does not parse or has an invalid field. Goose skips the whole
  file with a warning. `on_failure` does not cover load errors.

The `timeout` is in seconds. The example sets `600`, so the script's own 590
second deadline ends first and returns a clear deny.

## Install

1. Copy the plugin into Goose's user plugin folder:

```bash
mkdir -p ~/.agents/plugins/mezmo-governance/hooks
cp plugin.json mezmo_governance_hook.py ~/.agents/plugins/mezmo-governance/
cp hooks/hooks.json ~/.agents/plugins/mezmo-governance/hooks/
```

   Goose also loads `<project>/.agents/plugins/<name>/` when it starts in that
   project. With `GOOSE_PATH_ROOT` set, the user folder is
   `$GOOSE_PATH_ROOT/.agents/plugins/`.

2. Export the settings in the shell that starts `goose`:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

   Hook commands inherit Goose's environment. They run with `sh -c`, and
   Goose sets `PLUGIN_ROOT` to the plugin folder.

3. Run the [smoke test](#smoke-test), then one tool call your policy denies.

The rules in `hooks.json` have no `matcher`, so they run for every tool. A
matcher is a regular expression: a bare `"*"` is invalid, and Goose skips that
rule without blocking anything.

## Tool names in policy

Mezmo sees built-in tools with namespace `goose`. The `developer`, `analyze`,
`summon` and `code_execution` extensions expose bare names. Names seen live in
1.54.0: `shell`, `tree`, `analyze`, `write`, `read_image`, `load`,
`load_skill` and `delegate`. Goose has no file read tool: the model reads files
with `shell` or `analyze`.

Other extensions name tools `<extension>__<tool>`. The hook splits at the first
`__`, so `apps__delete_app` reaches Mezmo as namespace `apps`, tool
`delete_app` (seen live). An extension key that itself contains `__` splits in
the wrong place.

The hook denies Code Mode (the `code_execution` extension) scripts itself,
without asking Mezmo. The tool calls a script makes go to the extensions
directly, without a `PreToolUse` event. Verified live before this rule: with
`tree` denied, a script that called `Developer.tree` ran, and Mezmo saw only
`execute_typescript` and `list_functions`. So the hook blocks every
`execute_typescript` and `execute_bash` call, and Mezmo still decides
`list_functions` and `get_function_details`. To use Goose with this hook, turn
Code Mode off: run `goose configure`, choose Toggle Extensions, and disable
Code Mode.

## Smoke test

Run these from this directory. First check that the hook denies when Mezmo
cannot be reached. Nothing listens on port 9:

```bash
echo '{"event":"PreToolUse","session_id":"smoke","tool_call_id":"call_smoke","tool_name":"shell","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_ACCESS_KEY="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a block. The text after `unavailable:` varies by
platform:

```json
{"decision":"block","reason":"Mezmo Governance could not authorize this exact tool call, so Goose blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}
```

Then run the same line without the two inline variables, with your settings
exported. No output means Mezmo allowed the call.

To watch Goose itself, start `python3 tools/mock_governance.py --deny shell`
from the repository root, point `MEZMO_GOVERNANCE_URL` at it, and ask Goose to
run `ls`. The mock prints `denied goose:shell`, and Goose reports "Tool call
denied by policy hook `mezmo-governance`: Mezmo Governance denied this exact
tool call: mock policy: goose:shell is denied".

## Output

Allow prints nothing. Deny:

```json
{ "decision": "block", "reason": "Mezmo Governance denied this exact tool call: …" }
```

`request_id` is `goose:<session_id>:<tool_call_id>#<digest>`.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). Goose takes the call id
from the model provider, or makes a UUID when the provider sends none. A call
with an empty id is denied.

`PostToolUse` and `PostToolUseFailure` events only append the local JSONL
audit log, joined to the decision by `decision_id`. Goose 1.54.0 sends no tool
result, so `result_preview` is `null`, and a `PostToolUseFailure` is recorded
as `failed`.

## Tests

```bash
python3 -m unittest test_hook.py -v
```
