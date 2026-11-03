# Shell command parts for every agent

Date: 2026-11-03
Branch: `feat/bash-parts-all-agents`
Status: approved 2026-11-03

## Why

The Claude Code plugin sends a Bash call to Mezmo as one item per part of the
command, each with `program`, `arg_N` and `flag_<name>` fields a policy rule can
match (claude-code/README.md, "Bash commands"; docs/protocol.md, "Authorize
request"). Every other adapter still sends a shell call as one item whose
`arguments` hold the whole command string. A policy there can match only that
exact string or the whole tool (`droid:Execute`, `gemini_cli:run_shell_command`),
so shell is all or nothing: deny every command or allow every command. In
practice those agents can't be governed while they use their shell tool.

## Goal

Every adapter whose agent has a built-in shell tool sends that tool's calls the
way Claude Code sends Bash calls: same splitting, same fields, same safety
rules. A rule written for Claude Code (`program in [ls, cat, rg]`,
`flag_force eq true`) reads the same on any agent; only the tool it names
differs.

### In scope

| Adapter | Built as | Shell tool name(s) | Command field |
| --- | --- | --- | --- |
| Codex | one-file Python hook | `Bash` | `command` |
| GitHub Copilot | one-file Python hook | `bash` (CLI), `run_in_terminal` (VS Code), `Bash` (VS Code with a Claude-format `PreToolUse` entry) | `command` |
| Cursor | one-file Python hook | `Shell` (both `preToolUse` and `beforeShellExecution`) | `command` |
| Droid | one-file Python hook | `Execute` | `command` |
| Gemini CLI | one-file Python hook | `run_shell_command` | `command` |
| Goose | one-file Python hook | `shell` | `command` |
| Windsurf | one-file Python hook | `run_command` | `command_line` |
| Antigravity CLI | one-file Python hook | `run_command` | `CommandLine` |
| Claude Agent SDK (Python) | package | `Bash` | `command` |
| Claude Agent SDK (TypeScript) | `client.ts` + adapter | `Bash` | `command` |
| OpenCode | `client.ts` + adapter | `shell` (2.x), `bash` (1.x) | `command` |
| Pi | `client.ts` + adapter | `bash` | `command` |

Only calls in each adapter's own namespace count: an MCP or extension tool that
happens to be called `shell` or `run_command` is not split. A call whose
command field is missing or not a string goes whole. *(OpenCode 2.x's `shell`
input is assumed to hold `command`, as 1.x's `bash` does; the repo's tests
don't confirm it. If it doesn't, those calls go whole, as today. Worth checking
in the live run.)*

### Out of scope

- Mastra, LangChain and OpenAI Agents SDK. They have no built-in shell tool;
  their tools are whatever the user defines.
- Goose Code Mode (`execute_bash`, `execute_typescript`). The hook already
  denies these outright and keeps doing so.
- Copilot's and Pi's `powershell` tools, and any other PowerShell or cmd
  command (see Windows below).
- Claude Code's `PermissionDenied` counting. It is specific to Claude Code's
  auto mode.

### Anti-goals

These would make the change a failure even if splitting worked:

- **A parser problem blocks a call.** A command the parser can't split, a
  parser exception, or a command over the part limit goes as one item exactly
  as today. Splitting must never be the reason a call is denied.
- **A part gains a field it shouldn't.** The parser only ever takes fields
  away: a part it can't fully read gets no `program`, `arg_` or `flag_`
  fields, so an allow rule keyed on them can't match it. Every adapter keeps
  this lean.
- **Existing policies silently change meaning.** `tool_namespace` and
  `tool_name` stay each agent's own (`droid:Execute` stays `droid:Execute`),
  and the agent's own input fields stay on the first item.
- **Copies drift apart.** Twelve copies of a parser that disagree on where
  `&&` splits would make a rule mean different things on different agents.
- **Claude Code's requests change.** Its output stays byte-for-byte what it
  is today; its existing tests pass unchanged.

## What each request looks like

The item-building rules are Claude Code's `bash_items`
(claude-code/scripts/governance_hook.py), with the tool name and the command
field taken as inputs instead of hard-coded `Bash` and `command`:

- One item per part, ids from 0, at most 50 parts (`MAX_BASH_PARTS`); more
  than that goes as one item.
- Every item: `tool_namespace` is the adapter's `AGENT`, `tool_name` is the
  agent's shell tool name as received, and `arguments.command` is the part as
  written. When the part could be read: `program`, `arg_1` to `arg_10`, and
  one `flag_<name>` per flag, names as in Claude Code.
- An item after `&&`, `||`, `;` or a newline lists the previous id in
  `dependencies`.
- The first item also carries the agent's other input fields. When there is
  more than one part it carries `full_command`, the whole command.

**Agents whose command field isn't `command`** (Windsurf `command_line`,
Antigravity `CommandLine`): the first item keeps the agent's input exactly as
received, so `command_line` still holds the whole command and a rule on it
still matches, and adds `command` (the part), `full_command` when there is
more than one part, and the part fields. Later items
carry only `command` and the part fields, as in Claude Code. *(My call: it
keeps existing Windsurf and Antigravity rules working while giving them the
shared field names.)*

**Copilot's CLI payload** sends `toolArgs` as a JSON string; the adapter
already parses it before building the item. Splitting works on the parsed
object.

**Request identity is unchanged.** `request_id` and `decision_id` still hash
the call's whole input (`call_digest`), so a split call keeps the id it has
today.

## Windows

The parser reads POSIX shell syntax. On Windows most of these agents run
PowerShell or cmd, where `;`, `|`, quoting and `$` mean different things.

- On a Windows host (`os.name == "nt"` in Python, `process.platform ===
  "win32"` in TypeScript), adapters send shell calls whole, as today.
- Exception: the Claude Agent SDK's `Bash` tool runs Git Bash on Windows, as
  Claude Code's does, so it splits everywhere. Claude Code itself is unchanged.
- Copilot's `powershell` tool is never split, on any host.

The host OS decides, not the shell the tool actually runs (decided
2026-11-03). Agents that run the user's own shell (VS Code `run_in_terminal`,
Cursor, Windsurf, Codex) will split a fish or pwsh command on macOS or Linux as
bash. That is accepted: those shells' substitutions use `(`, `$(`, `$` or
backticks, which the parser already reads as unreadable or unsplittable, and
the risky misread (cmd's `%VAR%`) is Windows-only. Each in-scope README says
that a non-bash shell on macOS or Linux isn't detected.

Pi keeps the OS rule too, although its separate `powershell` tool suggests its
`bash` runs a real bash on Windows; that isn't confirmed here.

*(Leaning to fewer fields: a whole command can only fall to the policy's
tool-level rules, while a PowerShell command misread as bash could hand an
allow rule a `program` that isn't what runs.)*

## One parser, many copies

The repo already ships shared code as copies so each adapter installs on its
own, with drift tests that fail when a copy differs (claude-agent-sdk/python/
tests/test_drift.py, pi/test/drift.test.ts). This work follows that pattern.

**Python.** Claude Code's parser section in governance_hook.py is the source
copy: the constants, `BashPart`, `Unsplittable`, `split_command`,
`_skip_wrappers`, `_flags_of`, `_read_part`, `_Scanner`, and the generalised
item builder. Each one-file hook gets the same definitions pasted in, because
it is installed as one copied file (the reason the parser moved into
governance_hook.py in f7c1b30). Each copy's tests compare those top-level
definitions against Claude Code's by AST, as test_drift.py does, and skip when
the hook is copied out of the repo. For the Claude Agent SDK package, where the
definitions live (hooks.py, client.py or a new module) is the builder's call,
as long as the drift test covers them and the shared client.py copies in
langchain/ and openai-agents/ stay in step.

**TypeScript.** One port of the Python parser and item builder, placed in
`client.ts`, which pi/, opencode/, claude-agent-sdk/typescript/ and mastra/
already share under drift.test.ts. Putting it there adds no file to any
install: OpenCode loads every top-level `.ts` in its plugins folder, Pi loads
extension files, and doctor checks for exactly `client.ts`
(tools/doctor.py around line 1054 and 1092), so a new file would break or
complicate installs. Mastra carries the code unused. Each adapter tells
`approvalPayload` which tool name is its shell tool and which field holds the
command; how is the builder's call.

**Python and TypeScript agree.** One shared set of cases, each a command and
the items it must produce, run by both a Python test and a TypeScript test.
They skip when the case file isn't present (a copied directory). The cases
cover at least every example in Claude Code's README "Bash commands" tables and
the edge cases in claude-code/tests/test_bash_parts.py.

## Docs

- docs/protocol.md "Authorize request": the bullet that describes Claude Code's
  Bash parts covers every adapter, with the shell tool table above.
- Each in-scope adapter's README gets a short "Shell commands" section: which
  tool is split, the fields, the Windows rule, and a link to Claude Code's
  README for the full field and flag tables and the guardrail warning.
- Versions bump where an adapter carries one (plugin.json,
  gemini-extension.json, package.json, pyproject.toml).

## Left to the builder

- The generalised item builder's signature, and how Claude Code calls it.
  `bash_items({"command": ...})` must stay callable with one argument
  (claude-code/tests/test_key_file.py calls it that way). Python copies are
  verbatim, keeping Claude Code's `typing` spellings, so the AST comparison
  holds.
- How each adapter recognises its shell tool, within the table above.
- Where the shared case file lives and its format.
- Test layout for each adapter, matching that adapter's existing tests.
- Where the Claude Agent SDK Python package keeps its copy.
