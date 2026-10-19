# Mezmo Governance plugins

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](#requirements)
[![Node 22.18+](https://img.shields.io/badge/node-22.18%2B-5FA04E?logo=nodedotjs&logoColor=white)](#requirements)
[![Last commit](https://img.shields.io/github/last-commit/mezmo/ai-governance-plugins)](https://github.com/mezmo/ai-governance-plugins/commits/main)

Plugins that ask Mezmo Governance before a coding agent runs a tool. If Mezmo
denies the call, or never answers, the tool does not run. (The Claude Code
plugin needs a key first: see [Claude Code](#claude-code).)

To get access to Mezmo Governance, see [mezmo.com/platform/governance](https://www.mezmo.com/platform/governance).

```text
Coding agents: Claude Code, Codex, Cursor, Gemini CLI, Antigravity CLI,
               Copilot, Goose, Droid, Windsurf, Pi, OpenCode
Agent SDKs:    Claude Agent SDK, LangChain, OpenAI Agents SDK, Mastra
        │  before the tool runs
        ▼
  plugin  ──POST /governance/workflow/authorize──►  Mezmo Governance
        │
        │  { "approved": true|false, "reason"? }
        ▼
  run the tool, or stop it and show the reason to the agent
```

## Requirements

- Python 3.9+ for the `mezmo-governance` command and the hook adapters.
- Python 3.10+ for the LangChain, OpenAI Agents SDK and Claude Agent SDK
  libraries.
- Node 22.18+ for Pi, OpenCode, Mastra and the TypeScript Claude Agent SDK,
  and for their tests: they run `.ts` files by type stripping.
- macOS or Linux. Windows is not tested.

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

3. Ask Claude to run `ls`. The mock prints a `denied` line for `claude_code:Bash`, and
   Claude reports `Mezmo Governance denied this exact tool call: mock policy:
   claude_code:Bash is denied`. Ask it to read a file: it runs without reaching the mock,
   because the Claude Code plugin only sends Bash and MCP tool calls.

`--deny PATTERN` and `--hold PATTERN` repeat. A pattern matches the tool name
(`Bash`) or `<namespace>:<tool>` (`claude_code:Bash`), with shell wildcards,
and is case-sensitive: `--deny 'mcp__*'`, `--deny 'kubernetes:pods_*'`.
`--hold` leaves a call pending, so you can watch the plugin wait and then
deny. `--port` sets the port (default 8787).

## Connect to Mezmo

1. In Mezmo Governance, register one agent per coding tool, so each has its
   own policy and log. The add-agent form asks for:

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
2. Give the agent a service account: the form asks for one next. Create one
   only for Governance and give it Governance permissions and nothing else,
   so a leaked token cannot reach the rest of your Mezmo account. Leave out write
   access to policies and agents: the authorize call needs neither, and a
   governed agent can read its own token, so a token that can edit policies
   lets the agent change its own rules. Use a separate key for
   [`policy apply`](#set-up-rules). An account
   without Governance permissions gets `401 EAUTH` on every call.

   `MEZMO_GOVERNANCE_TOKEN` is the service account's token, a standard Mezmo
   service key, not a key of the agent. Create it under Settings >
   Organization > API Keys, as in [Generate Service and Ingestion Keys](https://docs.mezmo.com/docs/ingestion-key), and copy it when
   Mezmo shows it: the full key appears only once. This README calls it the
   service account token.
3. Add policies, as in [Create a policy](#create-a-policy), and assign one to
   the agent: an agent without one gets `403 EPOLICYUNASSIGNED`. The last
   page of the form shows a `config.toml` for Mezmo's own agent runtime,
   Aura. The plugins need only two values from it: the `url`, which is your
   `MEZMO_GOVERNANCE_URL`, and the token.
4. Give each agent the URL and the service account token. Most agents read
   environment variables, so export them in the shell that starts the agent:

   ```bash
   export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
   export MEZMO_GOVERNANCE_TOKEN="<service account token>"
   ```

   Claude Code also reads these variables. After step 5 you can instead run
   `/mezmo:setup governance`, which keeps the key in the system credential
   store. OpenCode 2.x takes them as plugin options in `opencode.json`
   (see [opencode/](opencode/)), because its tools run in a background
   service that does not see your shell.
5. Install the plugin for your agent, from this repository. The install
   governs every session of that agent on this machine, so try a new policy
   in one Claude Code session first, as in
   [A policy for an end-to-end check](#a-policy-for-an-end-to-end-check).
   See the change first with `--dry-run`:

   ```bash
   ./mezmo-governance install claude-code --dry-run
   ./mezmo-governance install claude-code
   ```

   Name one or more agents, or pass `--all` for every agent on your `PATH`.
   The names are `claude-code`, `codex`, `cursor`, `gemini-cli`, `copilot`,
   `goose`, `opencode`, `pi`, `droid`, `windsurf` and `antigravity`.
   `./mezmo-governance agents` lists them; [the table below](#the-mezmo-governance-command)
   says what `install` does for each.
6. Check your setup. `doctor` reads the two environment variables, even
   where the agent keeps its own copy, so export them in this shell first:

   ```bash
   ./mezmo-governance doctor
   ./mezmo-governance smoke
   ```

   Each `doctor` check prints one `ok`, `warn`, `FAIL` or `skip` line with a
   fix, and the exit code is 1 when a check fails. `smoke` sends each
   installed hook one shell call (it must be denied) and one read (it must be
   approved) through its own mock. See [doctor](#doctor) and [smoke](#smoke)
   for what each checks.
7. Run one tool call your policy denies, and check that the agent shows the
   reason. Then check `~/.mezmo/governance-audit.jsonl` for the approved calls.

To remove a plugin, run `./mezmo-governance uninstall <agent> --dry-run`, then
run it again without `--dry-run`.

### Create a policy

A policy has its own dialog in Mezmo Governance, in five pages:

1. **Basics:** a policy name and a description.
2. **Rules:** allow or deny rules. Each rule matches a tool space, a tool
   name and, optionally, the tool call's arguments. For a denied call, pick
   an error code and write the message the agent shows. The tool space is
   the plugin's namespace, such as `claude_code`, `codex`, `cursor`,
   `gemini_cli`, `antigravity`, `copilot`, `goose`, `droid`, `windsurf`, `opencode` or `pi`, or the MCP server
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
| 2 | `claude_code` | `Bash` | deny, with a message such as `Bash is blocked by policy` |
| Fallthrough | | | **Send for Approval**, with yourself as approver |

Run the check in one Claude Code session that loads the plugin, not with a
user-level install. An installed plugin governs every Claude Code session on
the machine: under this policy each one loses `Bash`, and every MCP tool call
waits for your approval. Install at user level once the policy fits your
daily work.

1. `./mezmo-governance doctor --probe`: every Mezmo line is `ok`.
2. Start `claude --plugin-dir ./claude-code` from this repository, with
   `MEZMO_GOVERNANCE_URL` and `MEZMO_GOVERNANCE_TOKEN` exported. Ask it to
   read a file: it runs, and nothing reaches Mezmo. The plugin only sends
   Bash and MCP tool calls.
3. Ask it to run `ls`: Claude Code shows your deny message.
4. Ask it to call an MCP tool, such as listing your Mezmo pipelines once
   `/mezmo:setup mcp` has connected Mezmo's MCP server: Claude Code waits;
   approve the call in Mezmo and it runs. Try once more and reject it: the
   call is blocked.

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

## Install options

### The mezmo-governance command

`./mezmo-governance help` lists the commands: `agents`, `install`,
`uninstall`, `doctor`, `mock`, `smoke` and `policy`. It needs Python 3.9 or later, and
Node 22.18+ for the OpenCode and Pi smoke tests.

`install` writes into each agent's user config folder and honors the agent's
own override: `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`,
`COPILOT_HOME`, `GOOSE_PATH_ROOT`, `XDG_CONFIG_HOME` (OpenCode),
`PI_CODING_AGENT_DIR` and `FACTORY_HOME_OVERRIDE` (Droid); Windsurf and Antigravity CLI have none. Before it edits a JSON file, it saves a copy next to it
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

| Agent | `install` name | `install` runs | Why this way |
| --- | --- | --- | --- |
| Claude Code | `claude-code` | `claude plugin install mezmo --marketplace <this repo> -s user`, after it uninstalls an older `mezmo-governance` plugin | `claude plugin install --help`: the marketplace may be "a path" |
| Codex CLI | `codex` | copies the hook, merges `codex/hooks.example.json` into `hooks.json` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Cursor | `cursor` | copies the hook, merges `cursor/hooks.example.json` into `~/.cursor/hooks.json` | `cursor-agent plugin` only adds marketplaces from a git URL |
| Gemini CLI | `gemini-cli` | `gemini extensions install ./gemini-cli --consent --skip-settings` | `gemini extensions install --help`: "from a git repository URL or a local path" |
| Copilot CLI | `copilot` | copies the hook and `hooks.example.json` to `~/.copilot/hooks/` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Goose | `goose` | copies the plugin to `~/.agents/plugins/mezmo-governance/` | `goose plugin install` takes a git URL whose root is the plugin |
| OpenCode 2.x | `opencode` | copies `opencode/` and names it in `opencode.json` with options; the key stays in a file you write | `opencode plugin add` takes an npm or git package only |
| Pi | `pi` | `pi install ./pi` | `pi install --help`: `pi install ./local/path` |
| Droid | `droid` | copies the hook, merges `droid/hooks.example.json` into `~/.factory/hooks.json`; refuses, changing nothing, when that would hide hooks Droid reads from `settings.json` or the older `hooks/hooks.json` | `doctor` and `smoke` check this file install; the native plugin is [below](#codex-cli-copilot-cli-and-droid) |
| Windsurf | `windsurf` | copies the hook, merges `windsurf/hooks.example.json` into `~/.codeium/windsurf/hooks.json`; refuses, changing nothing, when Windsurf would drop that file whole | Windsurf reads hooks only from `hooks.json` files |
| Antigravity CLI | `antigravity` | copies the hook, adds the `mezmo-governance` hook from `antigravity/hooks.example.json` to `~/.gemini/config/hooks.json`; refuses, changing nothing, when Antigravity would drop that file whole or it already has a `mezmo-governance` hook | `doctor` and `smoke` check this file install; an `agy plugin install` route was not built |

The Gemini CLI install runs with `GEMINI_CLI_TRUST_WORKSPACE=true` for that
one command, so Gemini asks nothing and adds no folder to `trustedFolders.json`.
Codex skips the hook until you trust it in `/hooks`. Pi loads the extension
from this checkout, and Claude Code keeps it as the marketplace, so keep the
checkout where it is. The
libraries (LangChain, OpenAI Agents SDK, Claude Agent SDK and Mastra) are set
up in app code; `install` points to their READMEs.

#### doctor

`doctor [agent...]` (`python3 tools/doctor.py` runs the same check for all agents) checks the settings, that Mezmo
answers and takes the key, and, for each coding agent on your `PATH`, its
version, the user-level install and its fail-closed setting. An install the
agent has turned off or does not trust fails: a disabled Claude Code plugin,
Gemini CLI extension or hook, or Goose plugin, and a Codex hook whose
trust Codex does not list as current, Droid's `hooksDisabled`, a Windsurf or
Antigravity CLI `hooks.json` that the agent would drop, and an Antigravity
CLI hook with `"enabled": false`. A Codex or Gemini CLI hook installed
before the guard learned to keep a deliberate block quiet gets a warning
with the reinstall step. It also runs
each installed Python hook against a closed port and checks that the hook
denies. A Codex, Cursor, Copilot, Goose, Droid, Windsurf or Antigravity CLI hook copy that differs from this
checkout gets a warning to run `install` again. A hook script whose `AGENT` names another
agent fails, as when `MEZMO_GOVERNANCE_HOOK` points at it: Mezmo then gets that agent's
`tool_namespace`, and this agent's rules never match. It reads the agent configs
and changes nothing. `info` lines give the add-agent form values from
step 1 of [Connect to Mezmo](#connect-to-mezmo). A key can pass the key check
and still belong to no agent. Add `--probe` to send Mezmo one test call for
the tool `doctor:probe`, which shows up in Mezmo like any other call.

#### smoke

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
- Droid: `droid plugin marketplace add mezmo/ai-governance-plugins`, then `droid plugin install mezmo-governance@ai-governance-plugins --scope user`.
  Droid names the marketplace after its source repository, `ai-governance-plugins`; `droid plugin marketplace list` shows it. Run live on 2026-10-17. See [droid/](droid/).

### Install by hand

Each adapter's README has the manual steps, the fallback when the command
does not fit your setup.

| Agent | Hook | Fails closed? | Install |
| --- | --- | --- | --- |
| [Codex CLI](codex/) | `PreToolUse` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `hooks.example.json`, trust it in `/hooks` |
| [Cursor](cursor/) | `preToolUse` hook | yes, with `failClosed: true` | copy the script, merge `hooks.example.json` |
| [Gemini CLI](gemini-cli/) | `BeforeTool` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | `gemini extensions install ./gemini-cli`, or copy the script and merge `settings.example.json` |
| [GitHub Copilot](copilot/) | `preToolUse` hook (CLI, cloud agent, VS Code on the Copilot and Local harnesses) | on a crash yes (VS Code Local needs the `\|\| exit 2` guard); on a timeout no | copy the script, add `hooks.example.json` to `~/.copilot/hooks/` |
| [Goose](goose/) | `PreToolUse` plugin hook | yes, with `on_failure: block` | copy the plugin to `~/.agents/plugins/mezmo-governance/` |
| [Droid](droid/) | `PreToolUse` command hook | on a crash yes (with the `\|\| exit 2` guard); on a timeout no | copy the script, merge `hooks.example.json` into `~/.factory/hooks.json` |
| [Windsurf](windsurf/) | `pre_run_command`, `pre_read_code`, `pre_write_code` and `pre_mcp_tool_use` hooks. Partial coverage: grep/find, `write_to_file`, web search, URL reads, git commit, deploy and subagents run ungoverned, and Restricted Mode loads no hooks | on a crash yes (with the `\|\| exit 2` guard); Windsurf sets no hook timeout | copy the script, merge `hooks.example.json` into `~/.codeium/windsurf/hooks.json` |
| [Antigravity CLI](antigravity/) | `PreToolUse` command hook | yes: a crash, a non-zero exit, output that is not JSON and a timeout all block | copy the script, add the `mezmo-governance` hook from `hooks.example.json` to `~/.gemini/config/hooks.json` |
| [Pi](pi/) | `tool_call` extension | yes | `pi install ./pi`, or copy the folder to `~/.pi/agent/extensions/` |
| [OpenCode](opencode/) 1.x and 2.x | `tool.execute.before` plugin | yes | 2.x: copy the folder, name it in `opencode.json` `plugins` with options; 1.x: copy to `~/.config/opencode/plugins/` |
| [LangChain](langchain/) v1 | `AgentMiddleware` (`wrap_tool_call` and `awrap_tool_call`) | yes | `pip install ./langchain` |
| [OpenAI Agents SDK](openai-agents/) (Python) | tool input guardrail on function and MCP tools; refuses hosted MCP; other hosted tools are not gated | yes | `pip install ./openai-agents` |
| [Mastra](mastra/) | agent `hooks.beforeToolCall` | yes | copy the files, pass `hooks` |
| [Claude Agent SDK](claude-agent-sdk/) (Python and TypeScript) | `PreToolUse` callback in the agent options | yes | Python: `pip install ./claude-agent-sdk/python`; TypeScript: copy the files; pass the hooks in `hooks` |

"Fails closed" means the tool does not run when the plugin itself crashes,
times out, or cannot start. Every plugin denies the call when Mezmo denies it,
cannot be reached, or returns something it does not understand.

Editors that run agents over ACP, such as Zed, run the agent's own hook:
Claude Code, Codex and Goose are governed there. Gemini CLI is not. See
[docs/live-testing.md](docs/live-testing.md#zed).

## Settings

| Variable | Required | Default |
| --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | none |
| `MEZMO_GOVERNANCE_TOKEN` | yes | none |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590`, the longest the plugin waits for a person to decide |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` |

The hook examples set a 600 second hook timeout, so the plugin's own deadline
ends first and returns a clear deny. Windsurf has no hook timeout. Antigravity CLI
honored a 600 second timeout live: the hook's own 590 second deadline ended
first and blocked the call with `approval still pending`.

## What leaves your machine

Before each tool call it governs (for Claude Code, each Bash and MCP tool
call), the plugin sends Mezmo the tool name and its full arguments: the shell command, the file path, and for write tools the file
content. It also sends the session id and the tool call id (Gemini CLI, Antigravity CLI, Copilot, Droid and Windsurf send a fresh random id per call, because they expose none; VS Code Local exposes one). Tool results are
not sent to Mezmo: post-tool hooks only append a line to a local file.

One exception reaches past Mezmo: the LangChain middleware records each
approved call as an OpenTelemetry span. With `trace_payloads=True` the span
also carries the full arguments and up to 8,000 characters of the result. The
span goes wherever the app's OpenTelemetry
exporter sends it; see [langchain/README.md](langchain/README.md).

Write policy on the assumption that Mezmo sees every argument, and keep secrets
out of tool arguments.

## Troubleshooting

Start with `./mezmo-governance doctor --probe` (or `python3 tools/doctor.py --probe`): it names most of the causes
below. It does not check the LangChain, OpenAI Agents SDK, Claude Agent SDK
or Mastra setup, which lives in app code; for those, run
[examples/check.sh](examples/).

| You see | Cause | Fix |
| --- | --- | --- |
| Every call runs and the mock prints nothing | the hook never ran | check the path in the hook config; Codex: trust the hook in `/hooks`; Claude Code: look for a `hook error` notice; Copilot: install in `~/.copilot/hooks/`, not `.github/hooks/`; Gemini CLI: use `~/.gemini/settings.json` or trust the folder; Goose: put the plugin in `~/.agents/plugins/` and use no `"*"` matcher; Droid: check the **Effective** tab of `/hooks` and trust the folder; Windsurf: trust the workspace, since no hook loads in Restricted Mode, and run `doctor`; Antigravity CLI: run `agy -p /hooks` and `doctor` |
| Every call is denied: `MEZMO_GOVERNANCE_URL must be a non-empty string` (Python) or `MEZMO_GOVERNANCE_URL is required` (TypeScript) | the agent did not inherit the variables | export them in the shell that starts the agent, or use the agent's own settings `env` block |
| `Mezmo Governance returned HTTP 401 EAUTH` | wrong or revoked service account token, or a service account without Governance permissions | give the agent's service account Governance permissions, then copy a fresh token from the service account in Mezmo |
| `Mezmo Governance returned HTTP 403 ENOAGENT: no agent is registered for these credentials` | Mezmo accepts the token, but no agent uses its service account | register an agent in Mezmo Governance and use the token of the service account that agent uses |
| `Mezmo Governance returned HTTP 403 EPOLICYUNASSIGNED: this agent has no policy assigned` | the agent exists but has no policy | assign a policy to the agent in Mezmo Governance |
| `Mezmo Governance returned HTTP 400 EINVAL: Bad Request` for one tool only | Mezmo accepts argument names made of letters, digits and `_` only, and the tool has one with `-` or `.` | none on the plugin side yet: the plugins send arguments exactly as the tool gets them; ask Mezmo to accept the name |
| `approval still pending` | policy needs a person and nobody decided in time | decide in Mezmo, or raise `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` (max 590) |
| `SyntaxError` or `TypeError` on start | `python3` is older than 3.9 | install a newer Python, or point the hook command at it |

The protocol, including polling and request identity, is in
[docs/protocol.md](docs/protocol.md).

## Tests

Python 3.9+ and Node 22.18+ (it runs `.ts` tests by type stripping). Tests
mock HTTP and never call Mezmo.

```bash
make test
```

Run one adapter with `make test-<folder>`, such as `make test-codex`, or the
`mezmo-governance` command's tests with `make test-tools`.
`make test-claude-code-mod` runs the Claude Code status display tests; it
needs the `claude` CLI and is not part of `make test`.

`make test` runs the LangChain middleware tests only when LangChain is
installed in `langchain/.venv` (see [langchain/](langchain/)), the
OpenAI Agents SDK guardrail tests only when the SDK is installed in
`openai-agents/.venv` (see [openai-agents/](openai-agents/)), and the Claude
Agent SDK transport tests only with `claude-agent-sdk/python/.venv` (see
[claude-agent-sdk/](claude-agent-sdk/)). Make each venv once, with
Python 3.10+:

```bash
(cd langchain && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
(cd openai-agents && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
(cd claude-agent-sdk/python && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
```

What the tests confirm, what they do not, and the live-run table for each
agent are in [docs/live-testing.md](docs/live-testing.md#what-the-tests-confirm).

## Security

Report a vulnerability as described in [SECURITY.md](SECURITY.md), not in a
public issue.

Apache License 2.0. See [LICENSE](LICENSE).
