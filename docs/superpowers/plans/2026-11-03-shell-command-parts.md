# Shell Command Parts for Every Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every adapter whose agent has a built-in shell tool sends that tool's calls to Mezmo as one item per part of the command, exactly as the Claude Code plugin sends Bash calls.

**Architecture:** Claude Code's parser in `claude-code/scripts/governance_hook.py` stays the source copy. Its `bash_items` gains a tool name and a command field, and a JSON file of cases generated from it pins its output. Each Python adapter gets a verbatim copy, checked by the AST drift tests in `codex/test_hook.py` and `claude-agent-sdk/python/tests/test_drift.py`. The TypeScript adapters get one port in the shared `client.ts`, checked against the same JSON cases. Each adapter decides only whether a call is its shell tool on a host where it splits.

**Tech Stack:** Python 3.9+ stdlib `unittest` (hooks), Python 3.10+ (Claude Agent SDK package), Node 24 `node --test` running `.ts` directly (erasable TypeScript only: no enums, no parameter properties).

**Spec:** `docs/superpowers/specs/2026-11-03-shell-command-parts-design.md`

## Global Constraints

- Claude Code's requests stay byte-for-byte the same: every existing test under `claude-code/tests/` passes unchanged, and `bash_items({"command": ...})` stays callable with one argument.
- Limits are Claude Code's: at most 50 parts (`MAX_BASH_PARTS = 50`), `arg_1` to `arg_10` (`MAX_BASH_ARGS = 10`), a `flag_` field name at most 64 characters (`ARGUMENT_NAME_MAX = 64`).
- `tool_namespace` is the adapter's `AGENT`; `tool_name` is the shell tool's name as received. Neither is renamed.
- Only a call in the adapter's own namespace is split. A call whose command field is missing or not a string goes whole.
- Any failure in splitting sends the call whole, exactly as today. Splitting never denies a call.
- `request_id` and `decision_id` are unchanged by splitting: they still hash the call's whole input.
- Windows: on a host where `os.name == "nt"` (Python) or `process.platform === "win32"` (TypeScript), shell calls go whole. The exception is the Claude Agent SDK's `Bash` tool, Python and TypeScript, which splits on every OS.
- `powershell` tools (Copilot, Pi) are never split.
- Python copies are verbatim: same names, same `typing` spellings (`List`, `Dict`, `Optional`, `Tuple`, `Union`, `Sequence`), so `ast.dump` matches Claude Code's. No `X | Y` outside annotations (the Python 3.9 floor test in `codex/test_hook.py` checks this).
- No new top-level `.ts` file in `pi/`, `opencode/` or `claude-agent-sdk/typescript/`, and no new file a user must copy.
- Each in-scope adapter README gets a `### Shell commands` section built from the template under Task 2, step 7, with that adapter's tool names filled in.
- Version bumps, minor (0.x.0 → 0.(x+1).0): `codex/.codex-plugin/plugin.json` 0.2.0, `copilot/plugin.json` 0.2.0, `droid/.factory-plugin/plugin.json` 0.2.0, `goose/plugin.json` 0.1.0, `gemini-cli/gemini-extension.json` 0.1.0, `pi/package.json` 0.1.0, `opencode/package.json` 0.1.0, `claude-agent-sdk/typescript/package.json` 0.1.0, `claude-agent-sdk/python/pyproject.toml` 0.1.0. Claude Code's version doesn't change.
- Run the whole suite with `make test` from the repository root before the last commit.

## Review Focus

1. **A split call keeps its identity.** `decision_id` and `request_id` must be the ones the same call gets when it goes whole, because the post-tool audit pairs on them. Task 2 compares the two payloads, Tasks 3 to 5 check that the `request_id` digest is the whole input's, and Task 6 compares `approvalPayload` with and without `shellCommandField`.
2. **Cursor's two event shapes.** `preToolUse` with `tool_name: "Shell"` and `beforeShellExecution` with a top-level `command` both split. Task 3 tests both.
3. **Copilot CLI's `toolArgs` is a JSON string.** It splits after parsing, the same as VS Code's object `tool_input`. Task 3 tests both payloads.
4. **A server tool named like the shell.** An MCP or extension tool called `shell`, `run_command` or `run_shell_command` goes whole. Tasks 3 and 4 test Gemini CLI (`mcp_context`), Goose (`<extension>__shell`) and Antigravity (`call_mcp_tool` with `ToolName: "run_command"`).
5. **Non-ASCII words and flags.** Python and TypeScript must agree on `-é`, `²>/dev/null` and a flag name over 64 code points. The cases file carries these, and Task 6 runs them through TypeScript.

---

### Task 1: Generalise Claude Code's item builder and pin it with shared cases

**Files:**
- Modify: `claude-code/scripts/governance_hook.py:583-618` (`bash_items`)
- Create: `claude-code/tests/bash_parts_cases.py` (case inputs plus a writer)
- Create: `claude-code/tests/bash_parts_cases.json` (generated)
- Modify: `claude-code/tests/test_bash_parts.py`

**Interfaces:**
- Produces: `bash_items(arguments: Mapping[str, Any], tool_name: str = "Bash", command_field: str = "command") -> Optional[List[Dict[str, Any]]]`. It reads the command from `arguments[command_field]` and puts `tool_name` on every item. Everything else is as today. The `rest` fields stay "every input field except `command`", so a `command_line` input keeps `command_line` on the first item.
- Produces: `claude-code/tests/bash_parts_cases.json`, a JSON array of `{"input": object, "tool_name": str, "command_field": str, "items": [item without "tool_namespace"] | null}`. `items` is `bash_items(input, tool_name, command_field)` with each item's `tool_namespace` key removed, or `null` when it returns `None`.
- Produces: `claude-code/tests/bash_parts_cases.py`, with `CASES: list[tuple[dict, str, str]]`, `expected(hook_module) -> list[dict]` and `main()`, which writes the JSON with `json.dumps(..., indent=2, ensure_ascii=False) + "\n"`.

- [ ] **Step 1: Write the failing tests** in `claude-code/tests/test_bash_parts.py`:

```python
class ItemBuilderTests(unittest.TestCase):
    def test_tool_name_and_command_field_are_inputs(self):
        items = hook.bash_items({"command_line": "cd /repo && ls", "cwd": "/repo"}, "run_command", "command_line")
        self.assertEqual([item["tool_name"] for item in items], ["run_command", "run_command"])
        self.assertEqual(items[0]["arguments"], {
            "full_command": "cd /repo && ls", "command_line": "cd /repo && ls", "cwd": "/repo",
            "command": "cd /repo", "program": "cd", "arg_1": "/repo",
        })
        self.assertEqual(items[1]["arguments"], {"command": "ls", "program": "ls"})
        self.assertEqual(items[1]["dependencies"], [0])

    def test_a_missing_command_field_goes_whole(self):
        self.assertIsNone(hook.bash_items({"command": "ls"}, "run_command", "command_line"))


class SharedCasesTests(unittest.TestCase):
    def test_the_cases_file_is_current(self):
        path = pathlib.Path(__file__).with_name("bash_parts_cases.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), bash_parts_cases.expected(hook))
```

Import `json` and `bash_parts_cases` (the tests directory is on `sys.path` under `unittest discover -s tests`).

- [ ] **Step 2: Run them and see them fail**

Run: `cd claude-code && python3 -m unittest tests.test_bash_parts -v`
Expected: FAIL. `bash_items()` takes 1 positional argument, and `bash_parts_cases` doesn't exist.

- [ ] **Step 3: Implement the two new parameters in `bash_items`.** It reads `arguments.get(command_field)` and sets `"tool_name": tool_name`. The docstring says the first item keeps the agent's input as received.

- [ ] **Step 4: Write `bash_parts_cases.py`.** `CASES` holds `(input, tool_name, command_field)` with `tool_name "Bash"` and field `"command"` unless stated. Cover at least these:
  - Every command in `test_bash_parts.py` and in the README "Bash commands" tables (`git push --force origin main`, `--force-with-lease=main`, `-rf`, `find . -delete`, `head -n5`, `kill -9`).
  - The wrappers, and `timeout -s KILL 10 npm test`.
  - `echo $HOME`, `ls > out.txt`, `ls 2>/dev/null`, `ls 2>&1 | head`, `(cd sub && make)`, `PATH=/tmp ls`, `if true; then rm -rf x; fi`, `*.sh`, `sudo rm -rf /`.
  - `echo $(whoami)`, `` echo `date` ``, `cat <<EOF\nhi\nEOF`, `diff <(ls a) <(ls b)`, `echo 'unclosed`.
  - `ls # c && rm x`, `"ls \\\n -la"`, `""`, `"> out.txt"`, `"&& ls"`, `"true; " * 51`, `echo 1 2 3 4 5 6 7 8 9 10 11 12`, `"ls --" + "a" * 70`.
  - `echo héllo -é`, `ls ²>/dev/null`, `"ls --" + "é" * 62`.
  - `{"command": "git push origin main && rm -rf /tmp/x", "description": "Push"}`.
  - `{"command_line": "cd /repo && ls", "cwd": "/repo"}` with `run_command` / `command_line`.
  - `{"CommandLine": "ls", "Cwd": "/"}` with `run_command` / `CommandLine`.
  - `{"command": 42}`, and `{"command_line": "ls"}` with field `command`.

  Then run `cd claude-code/tests && python3 bash_parts_cases.py` to write the JSON. Read the JSON and check by eye that `²>/dev/null` and the 51-part case come out as the source suggests: 51 parts gives `null`.

- [ ] **Step 5: Run all Claude Code tests**

Run: `make test-claude-code`
Expected: all pass, with no existing test edited.

- [ ] **Step 6: Commit**

```bash
git add claude-code/scripts/governance_hook.py claude-code/tests/bash_parts_cases.py claude-code/tests/bash_parts_cases.json claude-code/tests/test_bash_parts.py
git commit -m "claude-code: take the shell tool's name and command field in bash_items"
```

---

### Task 2: Codex splits `Bash` commands

**Files:**
- Modify: `codex/mezmo_governance_hook.py` (imports; parser section after `origin`; `SHELL_TOOLS`, `shell_command_field`; `approval_payload`)
- Modify: `codex/test_hook.py`
- Modify: `codex/README.md`, `codex/.codex-plugin/plugin.json`

**Interfaces:**
- Consumes: Task 1's `bash_items(arguments, tool_name, command_field)` and the definitions it uses, copied verbatim.
- Produces, the same in every Python hook:
  - `BASH_PARTS_DEFINITIONS`, a tuple in `codex/test_hook.py` naming everything copied: `MAX_BASH_PARTS`, `MAX_BASH_ARGS`, `ARGUMENT_NAME_MAX`, `SEQUENTIAL_SEPARATORS`, `SHELL_KEYWORDS`, `SIMPLE_WRAPPERS`, `ASSIGNMENT`, `GLOB`, `DURATION`, `NICENESS`, `STDBUF_OPTION`, `FILE_DESCRIPTOR`, `LETTERS`, `REDIRECTIONS`, `FlagValue`, `BashPart`, `Unsplittable`, `split_command`, `_skip_wrappers`, `_flags_of`, `_read_part`, `_Scanner`, `bash_items`.
  - `SHELL_TOOLS: dict[str, str]`, mapping a shell tool name to its command field.
  - `shell_command_field(tool_call: NormalizedToolCall) -> Optional[str]`. It returns `None` when `tool_call.namespace != AGENT` or `os.name == "nt"`, and `SHELL_TOOLS.get(tool_call.name)` otherwise.
  - `approval_payload` builds `items = (bash_items(args, name, field) if field else None) or [the single item it builds today]`.

- [ ] **Step 1: Write the failing tests** in `codex/test_hook.py`:

```python
class ShellCommandPartsTests(unittest.TestCase):
    def event(self, tool_name="Bash", command="cd /repo && rm -rf build"):
        return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_use_id": "t",
                "tool_name": tool_name, "tool_input": {"command": command}}

    def test_a_bash_call_goes_as_parts(self):
        items = hook.approval_payload(self.event())["items"]
        self.assertEqual(items, [
            {"id": 0, "tool_namespace": "codex", "tool_name": "Bash", "arguments": {
                "full_command": "cd /repo && rm -rf build", "command": "cd /repo", "program": "cd", "arg_1": "/repo"}},
            {"id": 1, "tool_namespace": "codex", "tool_name": "Bash", "arguments": {
                "command": "rm -rf build", "program": "rm", "arg_1": "build", "flag_r": True, "flag_f": True},
             "dependencies": [0]},
        ])

    def test_windows_sends_the_call_whole_with_the_same_identity(self):
        split = hook.approval_payload(self.event())
        with mock.patch.object(hook.os, "name", "nt"):
            whole = hook.approval_payload(self.event())
        self.assertEqual(whole["items"], [{"id": 0, "tool_namespace": "codex", "tool_name": "Bash",
                                           "arguments": {"command": "cd /repo && rm -rf build"}}])
        self.assertEqual((split["decision_id"], split["request_id"]), (whole["decision_id"], whole["request_id"]))

    def test_an_mcp_tool_goes_whole(self):
        items = hook.approval_payload(self.event("mcp__box__Bash"))["items"]
        self.assertEqual(len(items), 1)
```

Add `BASH_PARTS_DEFINITIONS` and this test to `SharedCoreDriftTests`. It skips when `claude-code/` is absent, as `test_shared_core_matches_the_claude_code_hook` does.

```python
    def test_bash_parts_match_the_claude_code_hook(self) -> None:
        path = HOOK_PATH.parent.parent / CLAUDE_CODE_HOOK
        if not path.is_file():
            self.skipTest("the Claude Code hook is not checked out next to this one")
        ours, claude_code = top_level_definitions(HOOK_PATH), top_level_definitions(path)
        for name in BASH_PARTS_DEFINITIONS:
            with self.subTest(definition=name):
                self.assertEqual(ours.get(name), claude_code[name])
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd codex && python3 -m unittest test_hook.ShellCommandPartsTests test_hook.SharedCoreDriftTests -v`
Expected: FAIL. Codex sends one item, and the definitions are missing.

- [ ] **Step 3: Copy the parser section.** Paste Claude Code's lines from the `# Bash command parts` comment through the end of `bash_items` after `origin`, plus `ARGUMENT_NAME_MAX = 64`. Add `import re` and the `typing` names the copy uses. Rewrite the section comment's last sentence to say the copy must match Claude Code's, which `codex/test_hook.py` checks.

- [ ] **Step 4: Add `SHELL_TOOLS = {"Bash": "command"}`, `shell_command_field`, and the `approval_payload` change.** The comment on `shell_command_field` says why Windows goes whole: these agents run PowerShell or cmd there.

- [ ] **Step 5: Run the Codex tests**

Run: `make test-codex`
Expected: all pass, including `PythonFloorTests`.

- [ ] **Step 6: Bump `codex/.codex-plugin/plugin.json` to 0.3.0.**

- [ ] **Step 7: Add `### Shell commands` to `codex/README.md`,** after "Tool names for policy". This is the template every adapter README uses. Fill in `<ns>:<tool>`, and add the per-adapter line where a later task gives one:

```markdown
### Shell commands

A `<ns>:<tool>` call goes to Mezmo as one item per part of its command, split
where Claude Code splits Bash commands (`&&`, `||`, `;`, `|`, `|&`, `&` and new
lines). Each part carries `command` and, when the plugin can read it,
`program`, `arg_1` to `arg_10` and a `flag_<name>` field per flag. The first
part also carries `full_command` and the call's other input. Mezmo decides
every part and goes with the strictest answer. The field and flag tables, and
what these fields can't stop, are in the Claude Code README's
[Bash commands](../claude-code/README.md#bash-commands).

On Windows the command goes whole, as one item, because the agent runs it in
PowerShell or cmd. A non-bash shell on macOS or Linux, such as fish, isn't
detected.
```

- [ ] **Step 8: Commit**

```bash
git add codex/
git commit -m "codex: send Bash commands as matchable parts"
```

---

### Task 3: Cursor, Copilot, Droid and Gemini CLI split their shell tools

**Files (each adapter: `mezmo_governance_hook.py`, `test_hook.py`, `README.md`, version file):**
- `cursor/`: `SHELL_TOOLS = {"Shell": "command"}`. No version file.
- `copilot/`: `SHELL_TOOLS = {"bash": "command", "run_in_terminal": "command", "Bash": "command"}`, version 0.3.0. README extra line: "`powershell` calls always go whole."
- `droid/`: `SHELL_TOOLS = {"Execute": "command"}`, version 0.3.0.
- `gemini-cli/`: `SHELL_TOOLS = {"run_shell_command": "command"}`, version 0.2.0.

**Interfaces:**
- Consumes: Task 2's parser copy, `SHELL_TOOLS`, `shell_command_field` and the `approval_payload` change. Copy them verbatim from `codex/mezmo_governance_hook.py`; only the `SHELL_TOOLS` value differs. Each hook keeps its own `approval_payload` signature: Copilot's is `(event, event_name)`.

- [ ] **Step 1: Write the failing tests,** one `ShellCommandPartsTests` class per adapter, in that adapter's own event shape and helper style. Each asserts these three things:
  1. `cd /repo && rm -rf build` gives the two items from Task 2's first test, with that adapter's `tool_namespace` and shell tool name.
  2. The split payload's `request_id` ends with `"#" + hook.call_digest(<AGENT>, <tool name>, <the whole input>)`. This works even where the id carries a fresh `run-<uuid>`. Under `mock.patch.object(hook.os, "name", "nt")` the call goes as one item.
  3. Each adapter's own extra case:
     - Cursor: a `beforeShellExecution` event `{"command": ...}` and a `preToolUse` event with `tool_name: "Shell"` each give two items.
     - Copilot: the CLI payload with `toolArgs` as the JSON string `"{\"command\":\"cd /repo && rm -rf build\"}"` and `toolName: "bash"` gives two items. A VS Code payload with `tool_name: "run_in_terminal"` gives two. `toolName: "powershell"` gives one.
     - Droid: `mcp__box__Execute` gives one item.
     - Gemini CLI: `run_shell_command` with `mcp_context: {"server_name": "box", "tool_name": "run_shell_command"}` gives one item.

- [ ] **Step 2: Run them and see them fail**

Run: `make test-cursor test-copilot test-droid test-gemini-cli`
Expected: the new tests fail with one item where two are expected.

- [ ] **Step 3: Copy the definitions and wire `approval_payload` in each hook.**

- [ ] **Step 4: Run the tests**

Run: `make test-cursor test-copilot test-droid test-gemini-cli test-codex`
Expected: all pass.

- [ ] **Step 5: Add each README's `### Shell commands` section from the template, and bump the versions.**

- [ ] **Step 6: Commit**

```bash
git add cursor/ copilot/ droid/ gemini-cli/
git commit -m "cursor, copilot, droid, gemini-cli: send shell commands as matchable parts"
```

---

### Task 4: Goose, Windsurf and Antigravity split their shell tools; all eight hooks stay identical

**Files:**
- `goose/`: `SHELL_TOOLS = {"shell": "command"}`, version 0.2.0. README extra line: "Code Mode's `execute_bash` is still denied outright."
- `windsurf/`: `SHELL_TOOLS = {"run_command": "command_line"}`. No version file. README extra line: "The first part keeps `command_line` as Windsurf sent it, so a rule on `command_line` still matches."
- `antigravity/`: `SHELL_TOOLS = {"run_command": "CommandLine"}`. No version file. The same extra line with `CommandLine`.
- Modify: `codex/test_hook.py` (sibling drift)

**Interfaces:**
- Consumes: Task 2's copy and wiring, the same as Task 3. Antigravity's signature is `approval_payload(event_name, event)`.
- Produces: `SHARED_DEFINITIONS` in `codex/test_hook.py` gains `*BASH_PARTS_DEFINITIONS` and `"shell_command_field"`, so all eight hooks are compared with each other.

- [ ] **Step 1: Write the failing tests,** with the same three assertions as Task 3. Each adapter's extra case:
  - Goose: `tool_name: "developer__shell"` gives one item.
  - Windsurf: `pre_run_command` with `tool_info: {"command_line": "cd /repo && rm -rf build", "cwd": "/repo"}`. The first item's arguments equal `{"full_command": ..., "command_line": "cd /repo && rm -rf build", "cwd": "/repo", "command": "cd /repo", "program": "cd", "arg_1": "/repo"}`.
  - Antigravity: `toolCall: {"name": "run_command", "args": {"CommandLine": ..., "Cwd": "/repo"}}` keeps `CommandLine` on the first item. `call_mcp_tool` with `ToolName: "run_command"` gives one item.

  In `codex/test_hook.py`, extend `SHARED_DEFINITIONS` as above.

- [ ] **Step 2: Run them and see them fail**

Run: `make test-goose test-windsurf test-antigravity test-codex`
Expected: the new adapter tests fail. The sibling drift test fails for goose, windsurf and antigravity on the new names.

- [ ] **Step 3: Copy and wire the three hooks.**

- [ ] **Step 4: Run all Python hook tests**

Run: `make test-codex test-claude-code test-cursor test-gemini-cli test-copilot test-goose test-droid test-windsurf test-antigravity test-tools`
Expected: all pass. `test-tools` covers `doctor` and the mock, which read these hooks.

- [ ] **Step 5: Add the READMEs and bump Goose's version.**

- [ ] **Step 6: Commit**

```bash
git add goose/ windsurf/ antigravity/ codex/test_hook.py
git commit -m "goose, windsurf, antigravity: send shell commands as matchable parts"
```

---

### Task 5: Claude Agent SDK (Python) splits `Bash` on every OS

**Files:**
- Create: `claude-agent-sdk/python/mezmo_governance_claude_agent_sdk/bash_parts.py`
- Modify: `claude-agent-sdk/python/mezmo_governance_claude_agent_sdk/hooks.py` (`hook_payload`)
- Modify: `claude-agent-sdk/python/tests/test_hooks.py`, `claude-agent-sdk/python/tests/test_drift.py`
- Modify: `claude-agent-sdk/README.md`, `claude-agent-sdk/python/pyproject.toml` (0.2.0)

**Interfaces:**
- Consumes: Task 1's definitions, copied verbatim into `bash_parts.py`, with `from mezmo_governance_claude_agent_sdk.client import AGENT`.
- Produces: `hook_payload` replaces `payload["items"]` with `bash_items(arguments)` when `split_tool_name(tool_name)` is `(AGENT, "Bash")` and that returns a list. No OS check. `client.py` doesn't change, so the `langchain/` and `openai-agents/` copies don't either.

- [ ] **Step 1: Write the failing tests.**
  - `test_hooks.py`: `hook_payload({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "cd /repo && rm -rf build", "description": "Clean"}}, "t")["items"]` has two items with `tool_namespace "claude_agent_sdk"`. The first has `full_command` and `description`.
  - With `os.name` patched to `"nt"`, the result is the same.
  - The payload's `request_id` ends with `"#" + call_digest("claude_agent_sdk", "Bash", <the whole input>)`.
  - `mcp__box__Bash` gives one item.
  - `test_drift.py`: a test comparing `top_level_definitions(PACKAGE / "bash_parts.py")` with Claude Code's for the names in `BASH_PARTS_DEFINITIONS`. Repeat the tuple here: the SDK's tests can't import Codex's. It skips when `claude-code/` is absent.

- [ ] **Step 2: Run them and see them fail**

Run: `make test-claude-agent-sdk`
Expected: the new tests fail.

- [ ] **Step 3: Create `bash_parts.py` and wire `hook_payload`.**

- [ ] **Step 4: Run the tests**

Run: `make test-claude-agent-sdk test-langchain test-openai-agents`
Expected: all pass.

- [ ] **Step 5: Add the README section for Python and TypeScript together,** with this Windows paragraph in place of the template's: "The `Bash` tool runs bash on every OS, Git Bash on Windows, so commands split there too." Bump `pyproject.toml`.

- [ ] **Step 6: Commit**

```bash
git add claude-agent-sdk/python claude-agent-sdk/README.md
git commit -m "claude-agent-sdk (python): send Bash commands as matchable parts"
```

---

### Task 6: TypeScript port in the shared `client.ts`

**Files:**
- Modify: `pi/client.ts`, then copy the change into `opencode/mezmo-governance/client.ts`, `mastra/client.ts` and `claude-agent-sdk/typescript/client.ts`, so `pi/test/drift.test.ts` still passes.
- Create: `pi/test/bash-parts.test.ts`

**Interfaces:**
- Produces, exported from `client.ts`:
  - `splitCommand(command: string): BashPart[] | null`
  - `type BashPart = { text: string; program: string | null; args: string[]; flags: [string, boolean | string][]; after: string | null }`
  - `bashItems(args: Record<string, unknown>, toolName = "Bash", commandField = "command"): Record<string, unknown>[] | null`. These are the same items as Python's, with `tool_namespace: AGENT`.
  - `approvalPayload(params)` gains `shellCommandField?: string`. When it is set and `splitToolName(params.toolName).namespace === AGENT`, `items` is `bashItems(params.arguments, tool.name, params.shellCommandField) ?? [the single item]`.

- [ ] **Step 1: Write the failing test** in `pi/test/bash-parts.test.ts`. It reads `../../claude-code/tests/bash_parts_cases.json` relative to the test file, and calls `t.skip` when the file is absent. For every case it calls `bashItems(case.input, case.tool_name, case.command_field)`, drops `tool_namespace` from each item, and runs `assert.deepEqual` against `case.items`. Add one `approvalPayload` test: the `cd /repo && rm -rf build` call with `toolName: "bash"` and `shellCommandField: "command"` gives two items. Without `shellCommandField` it gives one, with the same `decision_id`.

- [ ] **Step 2: Run it and see it fail**

Run: `cd pi && node --test test/bash-parts.test.ts`
Expected: FAIL, because `bashItems` is not exported.

- [ ] **Step 3: Port the parser and item builder into `pi/client.ts`** as a straight translation of Python's names and control flow. Use a `_Scanner`-shaped class and an `Unsplittable` error class. Where the languages differ, match Python:
  - `re.fullmatch` becomes an anchored regex, and `ASSIGNMENT.match` anchors at the start only.
  - `str.isdigit()` becomes `/^\p{Nd}+$/u`, and `body[:1].isalpha()` becomes `/^\p{L}/u`.
  - `len(field)` counts code points, `[...field].length`.
  - `str.strip()` becomes `trim()`.
  - `flags` stays a `Map`, so a repeated flag keeps its first position and its last value.
  - The whole split sits in `try/catch`, returning `null`.

- [ ] **Step 4: Run the cases**

Run: `cd pi && node --test test/bash-parts.test.ts`
Expected: PASS. Where a case fails, the TypeScript is wrong, not the JSON.

- [ ] **Step 5: Copy `pi/client.ts`'s new code into the other three copies and run every TypeScript suite**

Run: `make test-pi test-opencode test-mastra && cd claude-agent-sdk/typescript && node --test test/*.test.ts`
Expected: all pass, including `drift.test.ts`.

- [ ] **Step 6: Commit**

```bash
git add pi/client.ts pi/test/bash-parts.test.ts opencode/mezmo-governance/client.ts mastra/client.ts claude-agent-sdk/typescript/client.ts
git commit -m "client.ts: port the shell command parser"
```

---

### Task 7: Pi, OpenCode and the Claude Agent SDK (TypeScript) pass their shell tool; protocol docs

**Files:**
- Modify: `pi/mezmo-governance.ts`. It passes `shellCommandField: "command"` when `event.toolName === "bash"` and `process.platform !== "win32"`.
- Modify: `opencode/mezmo-governance.ts` (`gate`). It passes `shellCommandField: "command"` when `toolName` is `"bash"` or `"shell"` and `process.platform !== "win32"`.
- Modify: `claude-agent-sdk/typescript/mezmo-governance.ts`. It passes `shellCommandField: "command"` when `tool_name === "Bash"`, on every OS.
- Test: `pi/test/extension.test.ts`, `opencode/test/plugin.test.ts` (plus `v2.test.ts` for `shell`), `claude-agent-sdk/typescript/test/hooks.test.ts`
- Modify: `pi/README.md` (extra line: "`powershell` calls always go whole."), `opencode/README.md`, `docs/protocol.md`, and the three `package.json` versions (0.2.0)

**Interfaces:**
- Consumes: Task 6's `approvalPayload({ ..., shellCommandField })`.

- [ ] **Step 1: Write the failing tests,** one per adapter, through each adapter's existing fake fetch or authorize stub. Each checks three things:
  1. A shell call with `cd /repo && rm -rf build` posts two items, the second with `dependencies: [0]`.
  2. With `Object.defineProperty(process, "platform", { value: "win32" })`, restored in `finally`, Pi and OpenCode post one item, and the Claude Agent SDK still posts two.
  3. Pi's `powershell` posts one item. OpenCode 2.x's `shell` posts two.

- [ ] **Step 2: Run them and see them fail**

Run: `make test-pi test-opencode && cd claude-agent-sdk/typescript && node --test test/*.test.ts`
Expected: the new tests fail with one item.

- [ ] **Step 3: Wire the three adapters.**

- [ ] **Step 4: Run them**

Run: the same command as step 2.
Expected: all pass.

- [ ] **Step 5: Update the docs.**
  - `docs/protocol.md` "Authorize request": rewrite the Claude Code bullet to say that every adapter with a shell tool sends its calls as parts, with a table of adapter, shell tool, command field and Windows behaviour. The table is the spec's "In scope" table plus a Windows column.
  - Add the Pi and OpenCode README sections from the template. OpenCode's names both `opencode:shell` (2.x) and `opencode:bash` (1.x).
  - Bump `pi/package.json`, `opencode/package.json` and `claude-agent-sdk/typescript/package.json` to 0.2.0.

- [ ] **Step 6: Run the full suite**

Run: `make test`
Expected: all targets pass.

- [ ] **Step 7: Commit**

```bash
git add pi/ opencode/ claude-agent-sdk/typescript/ docs/protocol.md
git commit -m "pi, opencode, claude-agent-sdk (ts): send shell commands as matchable parts"
```
