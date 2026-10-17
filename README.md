# Mezmo Governance plugins

Plugins that ask Mezmo Governance before a coding agent runs a tool. If Mezmo
denies the call, or never answers, the tool does not run.

To get access to Mezmo Governance, see [mezmo.com/platform/governance](https://www.mezmo.com/platform/governance).

```text
Claude Code / Claude Agent SDK / Codex / Cursor / Gemini CLI / Copilot / Goose / Droid / Windsurf / Pi / OpenCode / LangChain / OpenAI Agents SDK / Mastra
        │  before the tool runs
        ▼
  plugin  ──POST /governance/workflow/authorize──►  Mezmo Governance
        │
        │  { "approved": true|false, "reason"? }
        ▼
  run the tool, or stop it and show the reason to the agent
```

## Try it in two minutes, without a Mezmo account

A local mock server stands in for Mezmo, so you can watch a plugin block a
call before you connect it to anything. You need Python 3.9+ and Claude Code
2.1.295+.

1. Start the mock and deny `Bash`:

   ```bash
   git clone https://github.com/mezmo/ai-governance-plugins
   cd ai-governance-plugins
   ./mezmo-governance mock --deny Bash
   ```

2. In a second terminal, start Claude Code with the plugin loaded:

   ```bash
   cd ai-governance-plugins
   export MEZMO_GOVERNANCE_URL=http://127.0.0.1:8787/governance/workflow/authorize
   export MEZMO_GOVERNANCE_TOKEN=local-test
   claude --plugin-dir ./claude-code
   ```

3. Ask Claude to run `ls`. The mock prints `denied claude_code:Bash`, and
   Claude reports `Mezmo Governance denied this exact tool call: mock policy:
   claude_code:Bash is denied`. Ask it to read a file: that call is approved.

Run `python3 tools/mock_governance.py --help` for patterns (`--deny 'mcp__*'`,
`--deny 'kubernetes:pods_*'`) and `--hold`, which leaves a call pending so you
can watch the plugin wait and then deny.

## Connect to Mezmo

1. In Mezmo Governance, create a policy and register one agent per coding
   tool, so each has its own policy and log. The add-agent form asks for:

   | Field | Example for Claude Code |
   | --- | --- |
   | Agent Name | `Claude Code on my-laptop` |
   | Description | `Claude Code with the Mezmo plugin` |
   | Runtime Name | `claude-code`, the adapter folder name |
   | Version | `2.1.300`, from `claude --version` |
   | Instance ID | `claude-code-my-laptop`, unique per machine |

   The plugins send none of these fields; Mezmo knows the agent by its
   service account's token.
   `./mezmo-governance doctor` prints these values for each agent on your
   `PATH`.

   Next the form asks for a service account. Create one only for
   Governance and give it Governance permissions and nothing else, so a
   leaked token cannot reach the rest of your Mezmo account. Leave out write
   access to policies and agents: the authorize call needs neither, and a
   governed agent can read its own token, so a token that can edit policies
   lets the agent change its own rules. Use a separate key for
   [`policy apply`](#set-up-rules). An account
   without Governance permissions gets `401 EAUTH` on every call. Then add
   policies, as in [Create a policy](#create-a-policy), and assign one to
   the agent: an agent without one gets `403 EPOLICYUNASSIGNED`. The last page shows a `config.toml` for Aura; the plugins need
   only two values from it, the `url` (your `MEZMO_GOVERNANCE_URL`) and
   `MEZMO_GOVERNANCE_TOKEN`.

   `MEZMO_GOVERNANCE_TOKEN` is the service account's token, not a key of the
   agent. Copy it from the service account in Mezmo.
2. Install the plugin for your agent, from this repository. The install
   governs every session of that agent on this machine, so try a new policy
   in one session first, as in
   [A policy for an end-to-end check](#a-policy-for-an-end-to-end-check).
   See the change first with `--dry-run`:

   ```bash
   ./mezmo-governance install claude-code --dry-run
   ./mezmo-governance install claude-code
   ```

   Name one or more agents, or pass `--all` for every agent on your `PATH`.
   `./mezmo-governance agents` lists them; [the table below](#the-mezmo-governance-command)
   says what `install` does for each.
3. Give it the URL and key. In Claude Code, run `/mezmo:setup governance`,
   which keeps the key in the system credential store. OpenCode 2.x takes them as plugin options in `opencode.json`
   (see [opencode/](opencode/)), because its tools run in a background
   service that does not see your shell. Every other agent reads environment
   variables:

   ```bash
   export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
   export MEZMO_GOVERNANCE_TOKEN="<service account token>"
   ```

4. Check your setup, with the variables exported:

   ```bash
   ./mezmo-governance doctor
   ./mezmo-governance smoke
   ```

   `doctor` (also `python3 tools/doctor.py`) checks the settings, that Mezmo
   answers and takes the key, and, for each coding agent on your `PATH`, its
   version, the user-level install and its fail-closed setting. An install the
   agent has turned off or does not trust fails: a disabled Claude Code plugin,
   Gemini CLI extension or hook, or Goose plugin, and a Codex hook whose
   trust Codex does not list as current, Droid's `hooksDisabled`, and a Windsurf
   `hooks.json` that Windsurf would drop. A Codex or Gemini CLI hook installed
   before the guard learned to keep a deliberate block quiet gets a warning
   with the reinstall step. It also runs
   each installed Python hook against a closed port and checks that the hook
   denies. A Codex, Cursor, Copilot, Goose, Droid or Windsurf hook copy that differs from this
   checkout gets a warning to run `install` again. It reads the agent configs
   and changes nothing. Each check prints
   one `ok`, `warn`, `FAIL` or `skip` line with a fix, and the exit code is 1
   when a check fails. `info` lines give the add-agent form values from
   step 1. A key can pass the key check and still belong to no agent. Add `--probe` to send Mezmo one test call for the tool
   `doctor:probe`, which shows up in Mezmo like any other call.

   `smoke` starts its own mock, sends each installed hook or plugin one shell
   call (it must be denied) and one read (it must be approved), and prints a
   line for each. The OpenCode plugin gets the options from `opencode.json`,
   as OpenCode passes them, with the mock's URL and key in place of yours;
   `smoke --live` keeps yours and reads each `{file:...}`. It calls no model and never sends your key.

   `smoke --live [agent...]` sends the same two calls, the agent's shell tool
   and its read tool, to Mezmo at `MEZMO_GOVERNANCE_URL` with
   `MEZMO_GOVERNANCE_TOKEN`. Your policy decides each call, so each line says
   allowed, denied with Mezmo's reason, or pending approval. A call held for
   approval waits 15 s unless you set `MEZMO_GOVERNANCE_TIMEOUT_SECONDS`. It
   exits 1 only when a call reached no decision, such as a `401`, an unknown
   agent or no connection. The calls show up in Mezmo like any other, but not
   in your local audit log.
5. Run one tool call your policy denies, and check that the agent shows the
   reason. Then check `~/.mezmo/governance-audit.jsonl` for the approved calls.

### Create a policy

A policy has its own dialog in Mezmo Governance, in five pages:

1. **Basics:** a policy name and a description.
2. **Rules:** allow or deny rules. Each rule matches a tool space, a tool
   name and, optionally, the tool call's arguments. For a denied call, pick
   an error code and write the message the agent shows. The tool space is
   the plugin's namespace, such as `claude_code`, `codex`, `cursor`,
   `gemini_cli`, `copilot`, `goose`, `droid`, `windsurf`, `opencode` or `pi`, or the MCP server
   name for an MCP tool. Each adapter README lists its tool names.
3. **Fallthrough:** what happens to a call that no rule permits:
   **Deny Outright**, **Send for Approval** or **Allow**.
4. **Approvers:** the people who decide calls that need approval, from a
   rule or from the fallthrough, and an optional Slack channel for those
   requests. A plugin waits for the decision up to
   `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` (590 by default), then denies.
5. **Review:** check the rules, then save.

### A policy for an end-to-end check

This policy tests all three outcomes with Claude Code:

| Rule | Tool space | Tool name | Effect |
| --- | --- | --- | --- |
| 1 | `doctor` | `probe` | allow, so `doctor --probe` passes |
| 2 | `claude_code` | `Read` | allow |
| 3 | `claude_code` | `Bash` | deny, with a message such as `Bash is blocked by policy` |
| Fallthrough | | | **Send for Approval**, with yourself as approver |

Run the check in one Claude Code session that loads the plugin, not with a
user-level install. An installed plugin governs every Claude Code session on
the machine: under this policy each one loses `Bash`, and every tool other
than `Read` waits for your approval. Install at user level once the policy
fits your daily work.

1. `./mezmo-governance doctor --probe`: every Mezmo line is `ok`.
2. Start `claude --plugin-dir ./claude-code` from this repository, with
   `MEZMO_GOVERNANCE_URL` and `MEZMO_GOVERNANCE_TOKEN` exported. Ask it to
   read a file: it runs.
3. Ask it to run `ls`: Claude Code shows your deny message.
4. Ask it to write a file: Claude Code waits; approve the call in Mezmo
   and the write runs. Try once more and reject it: the write is blocked.

### Set up rules

`./mezmo-governance policy` reads and adds rules through the Mezmo policy
API, so you can start without the policy dialog:

```bash
export MEZMO_GOVERNANCE_ADMIN_KEY="<key with Governance policy rights>"
./mezmo-governance policy show
./mezmo-governance policy apply
./mezmo-governance policy apply --yes
```

To try it first, start the mock and export its two lines, as in
[Try it in two minutes](#try-it-in-two-minutes-without-a-mezmo-account), and set
`MEZMO_GOVERNANCE_ADMIN_KEY=local-test`: `policy` then uses the mock's one agent
and policy.

`policy show` changes nothing. It prints each Mezmo agent with its runtime
and policy, each policy's rules (recourse, tool space or `*`, tool, message,
`rule_id`) and fallthrough, then for each agent installed here whether a rule
covers its shell tool and its read tool. Mezmo picks the policy from the
token's service account, and every agent here may send the same token. So
when Mezmo has one agent, every installed agent gets its policy. With several,
an installed agent maps to the Mezmo agents whose Runtime Name is its adapter
folder name, such as `claude-code`; pass `--agent <name or agent_id>` to use one
Mezmo agent's policy for all of them. A rule covers a tool when its tool and
tool space conditions (`eq`, `neq`, `in`, `nin`, or none) match the tool in
the agent's space and it checks no arguments. Mezmo decides from every
matching rule, not the first, so `show` prints the most restrictive one: deny,
then approval_required, then allow.

`policy apply [agent...]` plans two rules for the named agents, or for every
installed agent: deny their shell tools with `FORBIDDEN_TOOL` and the message
`Shell blocked by Mezmo policy`, and allow their read tools. Each rule names
the tool spaces of the agents it lists, so it leaves MCP tools and other
agents alone. Codex has no read-only tool (`apply_patch` writes files), so it
gets the deny rule only, and `smoke --live` sends it the shell call only. It leaves out
each tool a rule already covers and prints every rule it keeps, with its
`rule_id`. Without `--yes` it stops there. With `--yes` it opens a draft,
writes your rules back unchanged plus the new ones, and accepts the draft. If
the write or the accept fails, it deletes the draft. If the policy already has
an open draft, it changes nothing: finish or delete that draft in Mezmo first.

`policy` reads the key from `MEZMO_GOVERNANCE_ADMIN_KEY`, or asks for it in a
terminal, and never prints it. It never uses `MEZMO_GOVERNANCE_TOKEN`: every
governed agent can read that token, and an agent holding policy rights could
remove its own rules. Keep the admin key out of the shell that starts your
agents. The API is `https://api.mezmo.com/v3/governance`, or the host of
`MEZMO_GOVERNANCE_URL`, or `--url <base>`.

### The mezmo-governance command

`./mezmo-governance help` lists the commands: `agents`, `install`,
`uninstall`, `doctor`, `mock`, `smoke` and `policy`. It needs Python 3.9 or later, and
node for the OpenCode and Pi smoke test.

`install` writes into each agent's user config folder and honors the agent's
own override: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`,
`COPILOT_HOME`, `GOOSE_PATH_ROOT`, `XDG_CONFIG_HOME` (OpenCode),
`PI_CODING_AGENT_DIR` and `FACTORY_HOME_OVERRIDE` (Droid); Windsurf has none. Before it edits a JSON file, it saves a copy next to it
as `<name>.mezmo-backup-<time>` and prints the path. It writes through a
symlink and keeps the file's mode, such as `0600`. It never writes the
token. Running it twice changes nothing. Over an older install, it replaces
each copied file that differs from this checkout and prints `update <file>`.
Claude Code and Gemini CLI keep their own copy, so for them run `uninstall`,
then `install`. `uninstall` removes the entries and
files `install` added, then each key or file that leaves empty, such as an
empty `hooks` object, a Cursor `hooks.json` with only `version`, or Claude
Code's empty `enabledPlugins`, even one that was empty before the install. Codex keys hook trust by position, so where
other groups follow the Mezmo group in `hooks.json`, `uninstall` leaves an empty
group in its place and those hooks stay trusted. Where a Mezmo handler sits
ahead of your own handler in one group, `uninstall` changes no Codex file,
names the handlers that would move and how to remove the entry by hand, and
exits 1.

| Agent | `install` runs | Why this way |
| --- | --- | --- |
| Claude Code | `claude plugin install mezmo --marketplace <this repo> -s user`, after it uninstalls an older `mezmo-governance` plugin | `claude plugin install --help`: the marketplace may be "a path" |
| Codex CLI | copies the hook, merges `codex/hooks.example.json` into `hooks.json` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Cursor | copies the hook, merges `cursor/hooks.example.json` into `~/.cursor/hooks.json` | `cursor-agent plugin` only adds marketplaces from a git URL |
| Gemini CLI | `gemini extensions install ./gemini-cli --consent --skip-settings` | `gemini extensions install --help`: "from a git repository URL or a local path" |
| Copilot CLI | copies the hook and `hooks.example.json` to `~/.copilot/hooks/` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Goose | copies the plugin to `~/.agents/plugins/mezmo-governance/` | `goose plugin install` takes a git URL whose root is the plugin |
| OpenCode 2.x | copies `opencode/` and names it in `opencode.json` with options; the key stays in a file you write | `opencode plugin add` takes an npm or git package only |
| Pi | `pi install ./pi` | `pi install --help`: `pi install ./local/path` |
| Droid | copies the hook, merges `droid/hooks.example.json` into `~/.factory/hooks.json`; refuses, changing nothing, when that would hide hooks Droid reads from `settings.json` or the older `hooks/hooks.json` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Windsurf | copies the hook, merges `windsurf/hooks.example.json` into `~/.codeium/windsurf/hooks.json`; refuses, changing nothing, when Windsurf would drop that file whole | Windsurf reads hooks only from `hooks.json` files |

The Gemini CLI install runs with `GEMINI_CLI_TRUST_WORKSPACE=true` for that
one command, so Gemini asks nothing and adds no folder to `trustedFolders.json`.
Codex skips the hook until you trust it in `/hooks`. Pi loads the extension
from this checkout, and Claude Code keeps it as the marketplace, so keep the
checkout where it is. The
libraries (LangChain, OpenAI Agents SDK, Claude Agent SDK and Mastra) are set
up in app code; `install` points to their READMEs.

### Claude Code

```text
/plugin marketplace add mezmo/ai-governance-plugins
/plugin install mezmo@mezmo
```

Then run `/mezmo:setup governance`, which keeps the key in the system
credential store. Until it has a key, from setup or `MEZMO_GOVERNANCE_TOKEN`,
the plugin lets every call run; `MEZMO_GOVERNANCE_REQUIRED=1` makes it deny
instead. Details: [claude-code/](claude-code/).

### Codex CLI, Copilot CLI and Droid

The same repository is a marketplace for each, and each reads its own manifest
and gets its own adapter. Use this or `./mezmo-governance install`, not both:
both run the hook twice. `smoke` checks only the file install; `doctor` also checks the native Codex plugin.

- Codex CLI: `codex plugin marketplace add mezmo/ai-governance-plugins`, then `codex plugin add mezmo-governance@mezmo`, then trust both hooks in `/hooks`.
  **Until you trust them, Codex skips the hooks without a warning and every tool runs ungoverned** (seen live on 0.163.0).
  `doctor` asks `codex app-server` for the trust state and fails when the PreToolUse hook is not trusted or changed since you trusted it.
  For a headless setup, `codex app-server` method `hooks/list` gives each hook's key and current hash; add a
  `[hooks.state."<key>"]` table with `trusted_hash = "<hash>"` to `$CODEX_HOME/config.toml` for each.
- Copilot CLI: `copilot plugin marketplace add mezmo/ai-governance-plugins`, then `copilot plugin install mezmo-governance@mezmo`.
  A local-path marketplace loads the hook from that checkout, so editing it or switching its branch changes the hook.
- Droid: `droid plugin marketplace add mezmo/ai-governance-plugins`, then `droid plugin install mezmo-governance@<marketplace> --scope user`.
  Droid names the marketplace after its source; `droid plugin marketplace list` shows the name. Not run: see [droid/](droid/).

### Install by hand

Each adapter's README has the manual steps, the fallback when the command
does not fit your setup.

| Agent | Hook | Fails closed? | Install |
| --- | --- | --- | --- |
| [Codex CLI](codex/) | `PreToolUse` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `hooks.example.json`, trust it in `/hooks` |
| [Cursor](cursor/) | `preToolUse` hook | yes, with `failClosed: true` | copy the script, merge `hooks.example.json` |
| [Gemini CLI](gemini-cli/) | `BeforeTool` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | `gemini extensions install ./gemini-cli`, or copy the script and merge `settings.example.json` |
| [GitHub Copilot](copilot/) | `preToolUse` hook (CLI, cloud agent, VS Code) | on a crash yes; on a timeout no | copy the script, add `hooks.example.json` to `~/.copilot/hooks/` |
| [Goose](goose/) | `PreToolUse` plugin hook | yes, with `on_failure: block` | copy the plugin to `~/.agents/plugins/mezmo-governance/` |
| [Droid](droid/) | `PreToolUse` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `hooks.example.json` into `~/.factory/hooks.json` |
| [Windsurf](windsurf/) | `pre_run_command`, `pre_read_code`, `pre_write_code` and `pre_mcp_tool_use` hooks; Cascade has no hook for its other tools, such as search, web and git | on a crash yes (with the `\|\| exit 2` guard); Windsurf sets no hook timeout | copy the script, merge `hooks.example.json` into `~/.codeium/windsurf/hooks.json` |
| [Pi](pi/) | `tool_call` extension | yes | `pi install ./pi`, or copy the folder to `~/.pi/agent/extensions/` |
| [OpenCode](opencode/) 1.x and 2.x | `tool.execute.before` plugin | yes | 2.x: copy the folder, name it in `opencode.json` `plugins` with options; 1.x: copy to `~/.config/opencode/plugins/` |
| [LangChain](langchain/) v1 | `AgentMiddleware` (`wrap_tool_call` and `awrap_tool_call`) | yes | `pip install ./langchain` |
| [OpenAI Agents SDK](openai-agents/) (Python) | tool input guardrail on function and MCP tools; refuses hosted MCP; other hosted tools are not gated | yes | `pip install ./openai-agents` |
| [Mastra](mastra/) | agent `hooks.beforeToolCall` | yes | copy the files, pass `hooks` |
| [Claude Agent SDK](claude-agent-sdk/) (Python and TypeScript) | `PreToolUse` callback in the agent options | yes | Python: `pip install ./claude-agent-sdk/python`; TypeScript: copy the files; pass the hooks in `hooks` |

"Fails closed" means the tool does not run when the plugin itself crashes,
times out, or cannot start. Every plugin denies the call when Mezmo denies it,
cannot be reached, or returns something it does not understand.

## What leaves your machine

Before each tool call, the plugin sends Mezmo the tool name and its full
arguments: the shell command, the file path, and for write tools the file
content. It also sends the session id and the tool call id (Gemini CLI, Copilot, Droid and Windsurf send a fresh random id per call, because they expose none). Tool results are
not sent to Mezmo: post-tool hooks only append a line to a local file.

One exception reaches past Mezmo: the LangChain middleware records each
approved call as an OpenTelemetry span with the full arguments and up to 8,000
characters of the result. The span goes wherever the app's OpenTelemetry
exporter sends it; see [langchain/README.md](langchain/README.md).

Write policy on the assumption that Mezmo sees every argument, and keep secrets
out of tool arguments.

## Settings

| Variable | Required | Default |
| --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | none |
| `MEZMO_GOVERNANCE_TOKEN` | yes | none |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590`, the longest the plugin waits for a person to decide |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` |

The hook examples set a 600 second hook timeout, so the plugin's own deadline
ends first and returns a clear deny. Windsurf has no hook timeout.

## Troubleshooting

Start with `./mezmo-governance doctor --probe` (or `python3 tools/doctor.py --probe`): it names most of the causes
below. It does not check the LangChain, OpenAI Agents SDK, Claude Agent SDK
or Mastra setup, which lives in app code; for those, run
[examples/check.sh](examples/).

| You see | Cause | Fix |
| --- | --- | --- |
| Every call runs and the mock prints nothing | the hook never ran | check the path in the hook config; Codex: trust the hook in `/hooks`; Claude Code: look for a `hook error` notice; Copilot: install in `~/.copilot/hooks/`, not `.github/hooks/`; Gemini CLI: use `~/.gemini/settings.json` or trust the folder; Goose: put the plugin in `~/.agents/plugins/` and use no `"*"` matcher; Droid: check the **Effective** tab of `/hooks` and trust the folder; Windsurf: trust the workspace, since no hook loads in Restricted Mode, and run `doctor` |
| Every call is denied: `MEZMO_GOVERNANCE_URL must be a non-empty string` | the agent did not inherit the variables | export them in the shell that starts the agent, or use the agent's own settings `env` block |
| `Mezmo Governance returned HTTP 401 EAUTH` | wrong or revoked service account token, or a service account without Governance permissions | give the agent's service account Governance permissions, then copy a fresh token from the service account in Mezmo |
| `Mezmo Governance returned HTTP 403 ENOAGENT: no agent is registered for these credentials` | Mezmo accepts the token, but no agent uses its service account | register an agent in Mezmo Governance and use the token of the service account that agent uses |
| `Mezmo Governance returned HTTP 403 EPOLICYUNASSIGNED: this agent has no policy assigned` | the agent exists but has no policy | assign a policy to the agent in Mezmo Governance |
| `Mezmo Governance returned HTTP 400 EINVAL: Bad Request` for one tool only | Mezmo accepts argument names made of letters, digits and `_` only, and the tool has one with `-` or `.` | none on the plugin side yet: the plugins send arguments exactly as the tool gets them; ask Mezmo to accept the name |
| `approval still pending` | policy needs a person and nobody decided in time | decide in Mezmo, or raise `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` (max 590) |
| `SyntaxError` or `TypeError` on start | `python3` is older than 3.9 | install a newer Python, or point the hook command at it |

The protocol, including polling and request identity, is in
[docs/protocol.md](docs/protocol.md).

## Tests

Python 3.9+ and Node 22+. Tests mock HTTP and never call Mezmo.

```bash
make test
```

`make test` runs the LangChain middleware tests only when LangChain is
installed in `langchain/.venv` (see [langchain/](langchain/)), the
OpenAI Agents SDK guardrail tests only when the SDK is installed in
`openai-agents/.venv` (see [openai-agents/](openai-agents/)), and the Claude
Agent SDK transport tests only with `claude-agent-sdk/python/.venv` (see
[claude-agent-sdk/](claude-agent-sdk/)).

### What the tests confirm

Each adapter's tests feed it the payload its runtime sends and run the real
entry point: the hook script as a subprocess, or the plugin's exported hooks.
A local HTTP server stands in for Mezmo. The tests confirm that:

- an approved call runs unchanged, and a denied call is blocked with Mezmo's
  reason in the runtime's own deny format;
- the call is blocked when the URL or key is missing, Mezmo cannot be
  reached, a decision stays pending, or an answer arrives (or drips in)
  after the deadline; the Python hooks also block on malformed input from
  the runtime;
- the call is blocked when Mezmo's reply is not JSON, not an object, or has
  no boolean `approved`, and when a poll returns an unknown status;
- a `409` falls into polling, and a call without a call id never reuses an
  earlier decision;
- the timeout is capped below the runtime's hook timeout, and `nan`, `inf`
  and negative values are refused;
- the post-tool event writes an audit line and never blocks;
- each example config registers every event and blocks on hook failure,
  including the `|| exit 2` guard for Codex, Gemini CLI, Droid and Windsurf;
- `./mezmo-governance` installs each agent, finds it installed, changes
  nothing on a second install, and on uninstall leaves each config as it was,
  with the user's other entries and a backup; a run stopped between two steps
  is finished by the next one; `--dry-run` changes nothing; the key is in no
  file or output (`tools/test_mezmo_governance_cli.py`, with fake `claude`,
  `gemini` and `pi` binaries);
- the shared code is identical across the seven Python hook adapters, across the
  four `client.ts` copies, and between the LangChain Python client and its
  OpenAI Agents SDK and Claude Agent SDK copies; the Python hooks import on
  Python 3.9.

### What they do not confirm

The tests cannot show that a runtime calls the hook, or how it treats the
answer. That was checked by hand, against `tools/mock_governance.py`, with
one shell call (expected: denied) and one file read (expected: approved):

| Agent | Version | Deny and allow | Hook crash |
| --- | --- | --- | --- |
| Claude Code | 2.1.301 with the Mezmo plugin (2.1.296 with the earlier hook) | pass | blocked |
| Codex CLI | 0.162.1 | pass | blocked with the guard; runs without it |
| Cursor CLI | 2026.10.14 | pass | blocked |
| Gemini CLI | 0.64.0 | pass | blocked with the guard; runs without it |
| Copilot CLI | 1.0.95, 1.0.98 | pass, with user-level hooks | blocked (1.0.95) |
| Goose CLI | 1.54.0 | pass | blocked with `on_failure: block`, also on a timeout; runs without it |
| Droid | 0.244.0, read from its binary, not run | not checked | not checked |
| Windsurf | 1.126.0 (Devin Desktop 3.10.67), read from its binary, not run | not checked | not checked |
| OpenCode | 2.0.25 | pass, through the background service with plugin options | not checked |
| Pi | 1.3.0 | pass | not checked |
| Mastra | 1.77.0, `generate` and `stream` | pass | not checked |
| LangChain | 1.4.6, `invoke` and `ainvoke` | pass | not checked |
| OpenAI Agents SDK | 0.24.0, `run_sync`, `run_streamed` and `run` | pass | blocked, in the tests |
| Claude Agent SDK | Python 0.2.168, `query` and `ClaudeSDKClient`; TypeScript 0.3.301, `query` | pass | runs if a callback raises (Python, checked), so the callbacks catch every error |

The TypeScript plugins also pass `tsc --strict` against the real Pi, OpenCode
(1.19.0 and 2.0.25) and Mastra types. That check is not part of `make test`.

Not checked at all: a real Mezmo Governance endpoint, a decision a person
approves while the hook waits, Windows, the Copilot cloud agent and VS Code,
and how each runtime behaves when the hook times out. To repeat a live check,
start the mock and follow the smoke test in the adapter's README. For Mastra,
LangChain, the OpenAI Agents SDK and the Python Claude Agent SDK,
[examples/check.sh](examples/) runs a real agent and fails if the gate lets
the wrong tool run.

## Security

Report a vulnerability as described in [SECURITY.md](SECURITY.md), not in a
public issue.

Apache License 2.0. See [LICENSE](LICENSE).
