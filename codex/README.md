# Codex + Mezmo Governance

A Codex CLI command hook that asks Mezmo Governance before each tool runs. If
Mezmo denies the call, or the hook cannot get an answer, the hook denies and
Codex skips the tool. After an approved tool finishes, the same script appends
a local JSONL record. A second hook script sends a record of each tool
call to the Mezmo ledger service, with the same API URL and key
(see [Ledger](#ledger)).
The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

Official docs: https://developers.openai.com/codex/hooks

The script needs Python 3.9 or later and uses only the standard library.

## Getting started

1. `codex plugin marketplace add mezmo/ai-governance-plugins`
2. `codex plugin add mezmo-governance@mezmo`
3. Start Codex and ask it to "set up Mezmo". Its `mezmo-setup` skill asks for
   the Mezmo API URL (production by default) and creates a private file for
   you to paste the key into in your own editor. It then checks the key, saves
   it and connects the Mezmo MCP. See [Setup](#setup).
4. Start a new session, run `/hooks`, and trust all four Mezmo hooks.
5. Ask Codex to investigate an incident, such as "investigate PagerDuty
   incident Q34FCCWSTU9OI2". Its `mezmo-investigate` skill joins the
   incident's investigation and reads the Mezmo ledger before it changes
   anything. See [Incident investigations](#incident-investigations).

Codex picks a plugin's skill when your request matches it. To name one
yourself, type `$` and pick it, or run `/skills`: Codex lists them as
`mezmo-governance:mezmo-setup` and `mezmo-governance:mezmo-investigate`. For the [file install](#install), which has no skills, see
[Setup](#setup) and [Incident investigations](#incident-investigations).

## Setup

The `mezmo-setup` skill runs `mezmo_setup.py` for you. Run by hand,
`mezmo_setup.py` asks for two things: the Mezmo API URL (default
`https://api.mezmo.com`) and the key, a Mezmo service key read with
`getpass`, so it never shows on screen, in shell history, in a command line or
in an agent transcript. With no flags, run it in your own terminal, not
through Codex.

The skill runs it with flags instead, so nothing is asked:

| Flag | What it does |
| --- | --- |
| `--api-url URL` | The Mezmo API URL. Default: the saved one, else production |
| `--mcp-url URL` | The Mezmo MCP URL. Default: `https://mcp.mezmo.com/mcp` for the production API, none for another |
| `--key-file PATH` | Read the key from a private file: mode `0600`, in a `0700` folder, not a symlink. `python3 mezmo_key_file.py create-key-file` makes an empty one and prints its path |
| `--delete-key-file` | Delete the key file afterwards, on failure too |
| `--keep-key` | Use the key setup saved before, in the store `codex.json` names or, on macOS, the Keychain item below |
| `--no-ledger` | Save the ledger as off and skip its check |
| `--replace-mcp` | Replace a `[mcp_servers.mezmo]` setup did not write |
| `--status` | Show what is saved, never the key |
| `--remove` | Delete the key, `codex.json` and the MCP entry setup wrote |

The key never goes through the chat or a command line: governance records
each shell command's arguments, so a key there would end up in the trail.

It checks both before saving anything: governance with a status lookup that
records no decision, and the [ledger](#ledger) with an empty batch, which must
answer `202 {"accepted": 0}`. If either check fails, nothing is saved.
`--no-ledger` skips the ledger check and saves the ledger as off.

The key goes to the first store this machine has, and setup says which:

| Where | Store |
| --- | --- |
| macOS | the Keychain, service `mezmo-ai-governance`, account `codex`, written through `security -i` so the key is never in a command line |
| Linux with `secret-tool` and an unlocked keyring | the desktop keyring (libsecret) |
| Otherwise, and on Windows for now | `~/.mezmo/codex-key`, mode `0600`, in a `0700` folder |

The hooks refuse a key file that is a symlink or that anyone but you can
read or write. `~/.mezmo/codex.json` (mode `0600`) holds the API URL,
`ledger_enabled` and which store has the key, never the key. The hooks read
the key from the store on each call (about 20 to 25 ms for the Keychain) and
keep it in memory for that hook process only. A store they cannot read gives
a warning and counts as no key, so governance denies the call.

`setup codex --remove` deletes the key from its store and removes
`codex.json`. If the store refuses the delete, it says so, exits 1 and keeps
`codex.json`, so running it again still knows where the key is. When setup
moves the key to another store, it deletes the old copy only after the new
settings are saved, and a keyring that does not answer counts as no keyring.

From the API URL the hooks derive the governance URL,
`<api_url>/governance/workflow/authorize`, and the ledger URL,
`<api_url>/v1/audit/events`.

### The Mezmo MCP

[Investigations](#investigations) read the ledger through the Mezmo MCP
server's `get_ledger_investigation` tool, so setup also adds the server to
Codex's `config.toml` (`$CODEX_HOME`, default `~/.codex`) as `mezmo`:

```toml
# Written by Mezmo setup: run it again to change this table, or with --remove to delete it.
[mcp_servers.mezmo]
url = "https://mcp.mezmo.com/mcp"
http_headers_helper = "python3 /Users/you/.mezmo/codex-mcp/mezmo_mcp_headers.py"
enabled_tools = ["get_ledger_investigation", "list_ledger_entries"]
default_tools_approval_mode = "auto"
```

- Only the two ledger reads are on, and they run without asking. The
  server's other tools, including the ones that change pipelines, stay off.
- `http_headers_helper` is a command Codex runs with `sh -c` when it
  connects, and reads a JSON object of headers from its output. This one
  prints `{"Authorization": "Token <key>"}` from the key store setup used,
  so neither `config.toml` nor Codex's environment holds the key. A key in an
  environment variable (`bearer_token_env_var`) would have to be in the
  environment of Codex's background `app-server`, which keeps the one it
  started with.
- Setup copies the helper and `mezmo_settings.py` to `~/.mezmo/codex-mcp/`,
  so a plugin update does not move the path in `config.toml`.
- Setup changes only that table, and keeps the previous file as
  `config.toml.mezmo-backup`. If `config.toml` already has a `mezmo` server
  setup did not write, setup saves nothing and exits 4; `--replace-mcp`
  replaces it.
- For an API URL other than production, give `--mcp-url`, or setup skips the
  MCP and says so.

### Overrides

Environment variables win over what setup saved, value by value, for CI,
`codex exec` and development. They are plain text in your shell and in every
process it starts, the agent's tool calls included, so prefer setup.

| Variable | Overrides |
| --- | --- |
| `MEZMO_GOVERNANCE_URL` | The governance URL. The ledger URL then follows its host, unless `MEZMO_LEDGER_URL` is set |
| `MEZMO_GOVERNANCE_TOKEN` | The key |
| `MEZMO_LEDGER_URL` | The ledger URL, even with `ledger_enabled` false |
| `MEZMO_LEDGER_KEY` | The ledger's key, which is otherwise the governance key |

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

With neither variable nor setup, the URL is `https://api.mezmo.com` and there
is no key, so the hook denies every call.

Tested version: Codex CLI 0.162.1, run live against `tools/mock_governance.py`:
`Bash` was denied, and with the guard a hook crash blocked the call. The
native plugin's trust behavior was seen on 0.163.0.

## Fail behavior

Codex has no fail-closed setting. Codex 0.162.1 runs the tool when a hook
exits 1, for example on a crash (verified live). The example command ends
with a guard that turns any non-zero exit into exit 2, which Codex treats as
a block:

```bash
|| { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }
```

The hook exits 2 itself when it blocks on purpose, for example when a call is
still pending at its deadline. The guard keeps that exit and prints nothing
more, so the agent shows only the hook's reason. Any other non-zero exit, such
as a Python error or a missing `python3`, prints the message above. A missing
hook script makes Python exit 2 too; Python then names the file it could not
open.

With the guard, a crash blocks the call (verified live: `hook: PreToolUse
Blocked`). A wrong path or a missing `python3` also exits non-zero, so the
guard blocks those too. Codex still runs the tool when:

- the hook reaches its `timeout`, guard or not (verified live in 0.163.0);
- Codex skips the hook because it is new or changed and not yet trusted;
- the tool is hosted, such as `WebSearch`: it fires no hook.

The example sets the `PreToolUse` `timeout` to `600` seconds, so the script's own
590 second deadline ends first and returns a clear deny. Keep the `timeout` at
least 10 seconds above `MEZMO_GOVERNANCE_TIMEOUT_SECONDS`: if you lower one,
lower the other too, or a call still pending runs the tool. The 10 seconds
also cover the [ledger](#ledger): after a deny at the deadline, the hook may
spend up to 3 seconds more on reporting it.

Keep the guard on the governance `PreToolUse` command only. The ledger
commands end in `|| true`, so they can never block a call. Run the
[smoke test](#smoke-test) after each install, and check `/hooks` after each
config change.

## Install

`./mezmo-governance install codex` from the repository root does this for you, and `uninstall` undoes it. The steps below are the manual way.
Run them from the repository root. With `CODEX_HOME` set, use `$CODEX_HOME`
for `~/.codex`.

1. Copy both hook scripts, the governance hook and the ledger hook, the
   [investigation](#investigations) CLI, the settings loader both hooks
   import, and the setup command with its key-file and MCP helpers:

```bash
mkdir -p ~/.codex/hooks
cp codex/mezmo_governance_hook.py codex/mezmo_ledger_hook.py codex/ledger_investigation.py \
  codex/mezmo_settings.py codex/mezmo_setup.py codex/mezmo_key_file.py codex/mezmo_mcp_headers.py ~/.codex/hooks/
```

2. Merge [hooks.example.json](hooks.example.json) into `~/.codex/hooks.json`, or
   into `.codex/hooks.json` in a repo. Each event has two handlers: the
   governance hook first, then the ledger hook. The example paths start at
   `$HOME`, so they work from any directory. To keep the governance script
   elsewhere, set `MEZMO_GOVERNANCE_HOOK` to its absolute path. Codex runs
   hooks from the session directory, so a relative path breaks. To run the
   script without `python3` in front, run `chmod +x` on it first.

   The ledger script must sit beside the governance script, because each
   imports the other from its own folder. There is no `MEZMO_LEDGER_HOOK`
   override. If you set `MEZMO_GOVERNANCE_HOOK`, move both files to that
   folder and edit the path in the two ledger commands by hand.

3. Run `python3 ~/.codex/hooks/mezmo_setup.py` ([Setup](#setup)). The key
   is a standard Mezmo service key, not a key of the agent; see
   [Connect to Mezmo](../README.md#connect-to-mezmo). Or export the
   [overrides](#overrides).

4. Start Codex, run `/hooks`, and trust all four Mezmo hook entries: the
   governance hook and the ledger hook, each for `PreToolUse` and for
   `PostToolUse`. Until you trust them, Codex skips them and every tool runs
   ungoverned. Do not wait for a warning: Codex 0.163.0 gave none for the
   native plugin. Trust them again after each edit to the entries. Then run
   `./mezmo-governance doctor codex`: it fails while the governance
   PreToolUse hook is untrusted. It also shows where each setting comes from
   (environment, `codex.json`, key store or default), sends the ledger an
   empty batch, and warns when Codex skips a ledger entry.

Hooks are on by default. `hooks = false` under `[features]` in
`~/.codex/config.toml` turns them all off.

Codex inherits the environment of the shell that launches it, so
[overrides](#overrides) must be exported there.

Headless `codex exec` skips hooks that are not trusted yet, so the tool runs
ungoverned. Trust the hooks once in an interactive session first. For CI,
`codex exec --dangerously-bypass-hook-trust` runs them without stored trust.
It trusts every enabled hook, not just this one, so use it only where you
control every hook source.

To gate only some tools, change `matcher`. Examples: `Bash`, `^apply_patch$`,
`mcp__kubernetes__.*`. `apply_patch` also matches `Edit` and `Write`. The
ledger handler shares the group, so a narrower `matcher` also narrows what the
[ledger](#ledger) records.

### As a Codex plugin

```bash
codex plugin marketplace add mezmo/ai-governance-plugins
codex plugin add mezmo-governance@mezmo
```

Then start Codex and ask it to "set up Mezmo", or run
`./mezmo-governance setup codex` from a clone; both save the same settings.
Then trust all four hooks in `/hooks`. Until you do, Codex skips them without
a warning and every tool runs ungoverned (0.163.0). Use the plugin or the file
install, not both: both run the hook twice. `doctor` checks the plugin's
trust; `smoke` checks only the file install. For a headless setup, see
[Codex CLI, Copilot CLI and Droid](../README.md#codex-cli-copilot-cli-and-droid).

### Upgrading

A version without the ledger hook registered two hook entries; this one
registers four. What follows about trust was read from the Codex 0.167.0
source.

- File install: run `./mezmo-governance install codex` again, then trust the
  two new ledger entries in `/hooks`. The two governance entries stay trusted,
  so governance is not interrupted. If you installed by hand, copy both
  scripts again and add the ledger handler as the last handler of each Mezmo
  group, as in [hooks.example.json](hooks.example.json): Codex keys trust by
  position, and a handler added at the end moves no other. Until you trust
  the ledger entries, Codex skips them, so with the ledger on a denied call
  sends `not_run` with no `started` and an allowed call sends nothing.
- Native plugin: after any plugin update, Codex treats every hook of the
  plugin as changed, the governance hooks included. Each hook then fails with
  `plugin contents changed; hook approval required` and Codex runs the tool
  ungoverned, until you approve all four again in `/hooks`. Approve them
  right after each update, then run `./mezmo-governance doctor codex`.
- Changing a hook entry also makes Codex ask to trust it again. This version
  names the two ledger entries, "Recording the call in the Mezmo Ledger" and
  "Recording the result in the Mezmo Ledger", which `/hooks` showed as
  "Unnamed hook" before, so trust them again after the update, and after
  `./mezmo-governance install codex` for the file install.

## Verify

From the repository root, after setup (or with the two variables exported):

```bash
./mezmo-governance doctor codex
./mezmo-governance smoke codex
```

`doctor` checks the install, the settings and the key. `smoke` runs the
installed hook against its own mock: one shell call must be denied and one
read approved. Neither starts the agent.

To check that the agent calls the hook, run
`./mezmo-governance mock --deny Bash` in a second terminal. In the agent's
terminal, export the two lines it prints, start the agent, and ask it to run
`ls`. The
mock prints a `denied` line for `codex:Bash`. No line means the hook did not
run. Codex has no read-only tool, so `smoke --live` sends it the
shell call only.

## Smoke test

Run these from the `codex/` folder. First check that the hook
denies when Mezmo cannot be reached. Nothing listens on port 9:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_use_id":"smoke-1","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize" \
    MEZMO_GOVERNANCE_TOKEN="test" python3 mezmo_governance_hook.py
```

It exits 0 and prints a deny. The text after `unavailable:` varies by platform:

```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Mezmo Governance could not authorize this exact tool call, so Codex blocked it: Mezmo Governance is unavailable: <urlopen error [Errno 61] Connection refused>"}}
```

With the [ledger](#ledger) on, a deny in this section also sends a `not_run`
record for the made-up call. Run setup with `--no-ledger` first to avoid that.

Then check your key with the variables from step 3 set. This asks Mezmo about
one `echo hi` call:

```bash
echo '{"hook_event_name":"PreToolUse","session_id":"smoke","tool_use_id":"smoke-2","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | python3 mezmo_governance_hook.py
```

No output means Mezmo allowed the call. A deny prints the shape above with your
policy's reason. Use a new `tool_use_id` for each run: Mezmo returns the
earlier decision for an id it has seen.

## Tool names in policy

Mezmo sees the Codex tool name with namespace `codex`. Names seen live in
Codex 0.162.1: `Bash` for shell commands and `apply_patch` for file edits. An
MCP tool arrives with its server as the namespace. Codex has no read tool: it
reads files with `Bash` (`cat`), so a policy that denies `Bash` also blocks reads.

In Zed, Codex runs through `codex-acp`, which uses the same `CODEX_HOME` and trust store.
Trust the hooks in `codex` `/hooks` first: Zed shows no hook status, so an untrusted hook
is skipped without a sign. Set `CODEX_PATH` in `agent_servers.codex-acp.env` to your
installed `codex`; the adapter bundles an older one. Verified live in Zed 1.24.2.

### Shell commands

A `codex:Bash` call goes to Mezmo as one item per part of its command, split
where Claude Code splits Bash commands (`&&`, `||`, `;`, `|`, `|&`, `&` and new
lines). Each part carries `command` and, when the plugin can read it,
`program`, `arg_1` to `arg_10` and a `flag_<name>` field per flag. The first
part also carries `full_command` and the call's other input. Mezmo decides
every part and goes with the strictest answer. The field and flag tables, and
what these fields can't stop, are in the Claude Code README's
[Bash commands](../claude-code/README.md#bash-commands).

Existing rules on the shell tool's input may need rewriting, and this is a
breaking change for deny rules. When a command has more than one part,
`command` holds one part, the whole command is in `full_command` (a one-part
command has no `full_command`, and its `command` is the whole command), and the
call's other input, such as `cwd` or `description`, is on the first part only.
So a rule that matches a compound command's exact `command` string, looks in
`command` for text that spans a separator (`| sh` in `curl URL | sh`), or looks
for another input field on a later part, silently stops matching. A deny rule
like that now lets the command through, so add the same rule on `full_command`:
the strictest answer wins, and a deny on the first part denies the whole call.

On Windows the command goes whole, as one item, because the agent runs it in
PowerShell or cmd. A non-bash shell on macOS or Linux, such as fish, isn't
detected.

## Windows

The example command uses POSIX shell syntax (`${VAR:-default}`, `$HOME`) and
`python3`. On Windows, add `commandWindows` to each handler with an absolute
path. For `PreToolUse`, add the guard in PowerShell form:

```json
"commandWindows": "py -3 C:\\Users\\<you>\\.codex\\hooks\\mezmo_governance_hook.py; if ($LASTEXITCODE -ne 0) { exit 2 }"
```

The Codex docs do not say which shell runs `commandWindows`. The guard
assumes PowerShell; this is not verified.

In PowerShell, set the variables with `$env:MEZMO_GOVERNANCE_URL = "..."` and
`$env:MEZMO_GOVERNANCE_TOKEN = "..."` before you start Codex.

## What the hook sends

`tool_name` `mcp__kubernetes__pods_delete` becomes namespace `kubernetes` and
name `pods_delete`. Built-in tools (`Bash`, `apply_patch`, `update_plan`) use
namespace `codex`. The hook denies a call from an MCP server named `codex`: its
tools would pass as built-ins.

`request_id` is `codex:<session_id>:<turn_id>:<tool_use_id>#<digest>` when
`turn_id` is present. `<digest>` hashes the tool and its arguments.
`decision_id` is uuid5 of that string, so a retry of the same call polls the
existing decision instead of opening a second one, and an edited call gets its
own ([identity](../docs/protocol.md#identity)).

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
is still pending, the hook denies. On input it cannot read, such as broken or
too deeply nested JSON, or an unsupported event, it prints a deny and exits 2;
Codex treats exit 2 as a block.

## Ledger

The ledger keeps a record of the tool calls Codex makes. It is on whenever
the hooks have a key and a ledger URL, which [Setup](#setup) gives them, and
off when setup saved `ledger_enabled` false (`--no-ledger`). When it is on, Mezmo gets one record when each tool call is requested and one
when it ends. It records every tool Codex runs a hook for (matcher `.*`):
`apply_patch`, `Bash`, MCP tools, `update_plan` and the other local tools.

The ledger has its own hook script, `mezmo_ledger_hook.py`, registered as the
second handler of each event. Codex runs it at the same time as the
governance hook. After a deny Codex runs no `PostToolUse` hook, so the
governance hook reports that call to the ledger itself.

| Event | When |
| --- | --- |
| `agent.tool_call.failed` | A tool result states a failure. Codex 0.167.0 sends none; see [What Codex does not report](#what-codex-does-not-report) |
| `agent.tool_call.not_run` | The governance hook denied the call or could not authorize it |
| `agent.tool_call.ran` | Codex ran the `PostToolUse` hook for the call |
| `agent.tool_call.started` | Codex asked to run the tool |

Each record carries the session id, a call id shared by its start and end, and
the tool's name. The call id is the governance `decision_id`
([What the hook sends](#what-the-hook-sends)). `started` also carries the
tool's arguments. `ran` and `failed` carry the result, and the exit status
when the result states one. `not_run` carries why the call did not run.
Arguments and results are cut at 4,096 characters and are not redacted.

Each record also carries your git email (`git config user.email`) when one is
set, so the ledger can show who ran the session.

Each record also names the service and environment the call touched, such as
`checkout/prod`, or `unknown`, and whether the call reads or writes. Both are
worked out from the kubectl commands in a Bash call or from an MCP tool's name
and arguments, with `prod` when no namespace or environment is given.

The ledger URL is `<api_url>/v1/audit/events` and the key is the governance
key. The ledger service takes the account from the key, so there is no
account to set. These variables override that, as in [Overrides](#overrides):

| Variable | Purpose |
| --- | --- |
| `MEZMO_LEDGER_INCIDENT_KEY` | Incident the session is working on, such as `pagerduty:<incident id>`. Honored only when [`launch`](#investigations) set it |
| `MEZMO_LEDGER_KEY` | Mezmo service key. Optional, defaults to the governance key |
| `MEZMO_LEDGER_SPOOL` | Retry file. Optional, defaults to `~/.mezmo/ledger-spool.jsonl` |
| `MEZMO_LEDGER_URL` | Ledger events URL. Optional, defaults as above |

Without a key the ledger is off. Without `MEZMO_LEDGER_KEY`, the governance
key is sent to the ledger URL, whatever host that is. The key is sent as
`Authorization: Token <key>`, or as given when it starts with `Token ` or
`Bearer `. The URL must use `https`, or `http` to this machine only, and hold
no user name or password. For Mezmo, use
`https://<Mezmo API host>/v1/audit/events`.

Records the service refuses with a 400 or 403 are dropped. Records it refuses with a 401 are kept for retry,
because a fixed key will let them through.

The ledger hook never blocks a tool call or changes a governance decision: it
prints no hook output and always exits 0. If the ledger is slow or
unreachable, it stops waiting after 3 seconds and the call goes ahead.
Whatever it waits on, even a retry file another process holds locked, the
hook ends within about 4 seconds; a record still unsaved then is lost. Records
that cannot be sent go to the retry file, and each later run of the hook
resends up to 10 of them. So do records the ledger rejects one by one, and
records it refuses with a 408, 429 or 5xx. Records it refuses with any other
4xx are dropped. The retry file keeps at most 200 records and drops the
oldest past that. It holds the full records, including arguments and output,
and is readable only by you. Delete it to discard what is waiting.

Claude Code and Codex share the default retry file, and each run discards
records saved for a different ledger URL. A record is resent only with the
key it was saved with; it names that key by a short fingerprint, never the key
itself. If the two send to different URLs, give one its own
`MEZMO_LEDGER_SPOOL`.

### Governance refusals

When the governance hook denies a call or cannot authorize it, it sends
`not_run` itself. It does so only after it has printed the deny, so the
ledger cannot change or lose the decision, and it spends at most about 3
seconds on it. A record cut off at that limit is lost, and a slow send can
deliver a record twice. Nothing is recorded when the deadline watchdog ends
the hook, or for input the hook cannot read.

### Investigations

Agents working the same incident can share one investigation. Their records
carry the same incident key, and the ledger service files them under the
investigation it works out from that key and the key's account, so a second
agent can read what the first did, with the `get_ledger_investigation` MCP
tool, before it acts. Every session on the account that uses the same incident
key lands in the same one.

| Event | When |
| --- | --- |
| `agent.investigation.closed` | The session ran `ledger_investigation.py close` |
| `agent.investigation.joined` | The session ran `ledger_investigation.py join` |
| `agent.investigation.opened` | `ledger_investigation.py launch` started a command for an alert |

Each carries the incident key. `joined` and `closed` also
carry the session id, and `opened` carries `opened_by: "alert"`. `joined` and
`opened` carry `service` when the command gave `--service`: the deployment or
app the incident is about, such as `checkout`, as a DNS label.

An incident key is `<source>:<id>`, with source `jira`, `pagerduty` or
`slack`, such as `pagerduty:Q34FCCWSTU9OI2`. The CLI,
`ledger_investigation.py`, has three commands:

| Command | What it does |
| --- | --- |
| `close --incident-key <key>` | Prints one line, `{"action": "closed", "incident_key": ...}` |
| `join --incident-key <key> [--service <name>]` | Prints one line, `{"action": "joined", "incident_key": ..., "service": ...}`, `service` only when given |
| `launch --incident-key <key> [--service <name>] -- <command...>` | Sends `opened`, then runs the command in the investigation |

`join` and `close` need no setting and send nothing themselves. The ledger hook does the rest when a `Bash` call starts by running
the CLI with a Python interpreter, as `python3 <cli> join --incident-key <key>`,
and its output has that line for the same key. So run `join` and `close` each
on its own as the whole call, with the key written out: one after a `cd ... &&`,
or with a key passed through a shell variable such as `"$KEY"`, is not
recognized. Codex gives the hook no exit code, so the line is the sign the
command worked.

- After a `join`, the hook writes the investigation to the
  [session state](#session-state) and then sends `joined`. Every later call of
  the session carries the incident key, and so does the join call's own `ran`.
  If the state can't be written, it warns on stderr and sends no `joined`.
- After a `close`, it sends `closed` and clears the session's investigation,
  but only when it is the one closed.

With the plugin, ask Codex to investigate: the `mezmo-investigate` skill
follows the steps below, and runs the CLI from the plugin's own folder.

The file install has no skills, so its instructions go in `AGENTS.md`. The CLI
is at `~/.codex/hooks/ledger_investigation.py`, where
`./mezmo-governance install codex` copies it. Paste this block into
`AGENTS.md` and put the CLI's path in place of `<cli>`:

```markdown
## Incident investigations

Record your work on an incident in its Mezmo investigation. Below, `<cli>` is
the path of `ledger_investigation.py`.

1. Work out the incident's key: `pagerduty:<incident id>`, else
   `jira:<issue key>`, else `slack:<channel id>`. One incident can have all
   three, so when you start from a Slack channel or a Jira issue, use your
   PagerDuty, Slack and Jira tools to find its PagerDuty incident, and use that
   when there is one. Two agents that pick different keys get different
   investigations and never see each other's work.
2. Run `python3 <cli> join --incident-key <key> --service <name>` on its own,
   as the whole shell call, with the key written out in the command: no `cd` or
   other command before it, and no shell variable for the key. Do this even when
   the session was started by `ledger_investigation.py launch`. `--service` is
   the service the incident is about, from the incident itself, such as the
   PagerDuty incident's service or its alert's source. Give the deployment or
   app name, not a display name: `checkout`, not `Checkout Production`. If you
   aren't sure which service it is, leave `--service` out rather than guess.
3. Before you change anything, call the `get_ledger_investigation` MCP tool
   with `incident_key` set to the key, and read what was already done.
4. Run one command per shell call while you work on the incident: no `;`,
   `&&` or `||` chains. The ledger records one entry per tool call, so
   separate calls give each action its own entry, outcome and approval.
5. Stay on the incident's service and namespace: act only on the service and
   environment the incident names. If a command fails because a target isn't
   found, report it and stop. Don't search the cluster for another deployment
   with the same name and act on that.
6. When you are done, run `python3 <cli> close --incident-key <key>` with the
   same key, on its own as the whole shell call, with the key written out.
```

`launch` is for an alert that starts an agent, for example
`python3 ~/.codex/hooks/ledger_investigation.py launch --incident-key <key> -- codex exec "<prompt>"`.
It sends `opened` with `opened_by: "alert"`, then runs the command in its
place with `MEZMO_LEDGER_INCIDENT_KEY` set, and `MEZMO_LEDGER_LAUNCHED` set to
a marker tied to that key, so `launch` exits with the command's exit code. It uses the same settings as the hooks: what setup
saved, or the [overrides](#overrides) exported in the shell it runs from.
With the ledger off (no key, or `ledger_enabled` false) it runs nothing.
An `opened` that can't be sent is saved for retry and the command still runs.

The ledger hook takes the incident key from the environment only with that
marker. A `MEZMO_LEDGER_INCIDENT_KEY` set any other way, such as one left from
testing, is ignored, and the hook says so once per session on stderr.
Otherwise it would file every call made outside an investigation under that
incident. `doctor codex` warns of it too, and of a `MEZMO_LEDGER_ACCOUNT`,
which no release since 0.6.0 reads.

Codex runs hooks in its `app-server` process, which keeps the environment it
started with, even for a new terminal. So the variables `launch` sets may
never reach the ledger hook, and a variable you unset in your shell can still
be set there: `pkill -f "codex app-server"` resets it. That is why a launched
session still runs `join`: the session state wins over the environment. When
the variables do reach the hook, every call of the session carries the
investigation, even after `close`.

### References

After a `mezmo_memory_brief` call or one of the Insights reads below succeeds,
the ledger hook saves a small reference to what Codex read in the
[session state](#session-state). Every later tool call record of the session
carries the list as `references`, so the ledger can show what Codex knew when
it acted. The read's own record doesn't carry it: the next call's `started`
does.

| Type | Saved from | Fields |
| --- | --- | --- |
| `insights` | One of the Insights reads, one per call | `id`: the first `template_id`, `edge_key` or `trace_id` in the result, in that order, else a hash of the tool's name and arguments. `read_at`: when it was read. `tool`: the tool's name |
| `memory` | `mezmo_memory_brief`, one per record and one per remediation | `record`: the record's id. `version`: its version |

The list is the session's, newest last, and keeps at most 20: a read of the
same thing again moves it to the end, and past 20 the oldest go. Joining or
closing an investigation doesn't clear it. Only these small objects are saved,
never a tool's arguments or output.

The Insights reads are a fixed list of 15 tools:
`find_degraded_service_edges`, `find_log_patterns`,
`find_metric_analysis_anomalies`, `get_log_dimension_changes`,
`get_log_overview`, `get_log_pattern_series`, `get_metric_analysis_overview`,
`get_metric_analysis_series_values`, `get_service_failure_rates`,
`get_service_failures`, `get_service_graph`, `get_trace_spans`,
`list_failed_traces`, `list_metric_analysis_series` and
`list_service_latency_summaries`. They and `mezmo_memory_brief` are matched by
the tool's name on any MCP server, so another server's tool by one of these
names is counted too, and a tool Mezmo adds later is not recognized until the
list is updated.

### What Codex does not report

Read from the Codex 0.167.0 source. Codex gives hooks no exit code and no
duration, and it runs `PostToolUse` only for a call it counts as successful.

| Call | Ledger records |
| --- | --- |
| A `Bash` command that exits non-zero, times out or is denied by the sandbox | `started`, then `ran` with no `exit_status` |
| A call rejected at the approval prompt, a failed `apply_patch`, `apply_patch` typed into the shell (recorded as `Bash`), a long command that nothing polls, an interrupted call, or an MCP call that returns an error | `started` only |
| A hosted tool such as `WebSearch`, or an unknown tool | Nothing: no hook runs |
| A long command that a poll in a later turn sees finish | `started`, and a `ran` with another call id and only that poll's output |

`ran` and `failed` carry `duration_ms`, measured by the plugin: the time from
governance's approval of the call to the start of the ledger's `PostToolUse`
hook. It includes the tool's run time, any wait at the Codex approval prompt,
and a few tens of milliseconds to start the hook. It leaves out the governance
check and any approval wait in Mezmo. A call has no `duration_ms` when no
start time was saved for it, as when the ledger was off at approval, or when
it is a long command that a poll in a later turn sees finish. The timestamps
of `started` and `ran` give the time from request to result, the approval
wait included.

The local audit log and the ledger can disagree. For a `Bash` output that is
itself JSON, such as `{"exit_code":1}`, the audit log may say `failed` while
the ledger says `ran`: the ledger never takes an exit code from a `Bash`
string. The only `Bash` output it reads is the result line of an
[investigation](#investigations) `join` or `close`.

### Session state

The ledger hook reads a session's investigation and references from
`~/.mezmo/governance/codex-sessions/<session id>.json`, when that file exists.
An investigation there wins over `MEZMO_LEDGER_INCIDENT_KEY`. The ledger
hook writes the investigation there after an [investigation](#investigations)
`join` and clears it after a `close`, and adds the [references](#references)
of each Memory or Insights read, keeping whatever else the file holds. It
also notes an ignored `MEZMO_LEDGER_INCIDENT_KEY` there, so it warns of it
once per session. A call
that reads neither writes nothing there. The file is readable only by you
(file 0600, folder 0700). Nothing else in this plugin writes these files, and
nothing in this plugin removes them, so any session that joined an
investigation or read Memory or Insights keeps its file. They are small.
`MEZMO_GOVERNANCE_STATE_DIR` moves the folder. Claude Code reads the same
variable, so with it set the two share one folder, and Claude Code's 7 day
prune also removes old Codex files there. That is harmless.

While the ledger is on, the governance hook has the ledger script write one
small file for each call it approves,
`~/.mezmo/governance/codex-calls/<decision id>`, holding the time of the
approval. With `MEZMO_GOVERNANCE_STATE_DIR` set, the folder is `codex-calls`
inside it. While the ledger is off, it writes nothing there. The
ledger hook reads and deletes a call's file when the call ends, for
`duration_ms`. A call that never reports back leaves its file, so the ledger
hook also deletes any file there older than 7 days.

## Troubleshooting

Run `./mezmo-governance doctor --probe codex` from the repository root.
[Troubleshooting](../README.md#troubleshooting) in the root README lists each
error and its fix. The top cause for Codex: a hook not trusted in `/hooks`.
`doctor` also checks the Mezmo MCP: the server's URL, how it authenticates,
and an MCP `initialize` call with the key.

- **Every call is denied with "no Mezmo key"**: the hooks were trusted before
  setup saved a key, so governance denies the setup command too. Quit Codex,
  start it once with `codex --disable hooks`, ask it to set up Mezmo, then
  start it normally. Or run `./mezmo-governance setup codex` from a clone.
- **Setting up without Codex**: `./mezmo-governance setup codex` from a clone
  of this repository runs the same `mezmo_setup.py` in your terminal. The
  plugin's copy is in Codex's plugin cache, under a folder named for the
  plugin version, so it moves with each update.

## Tests

```bash
python3 -m unittest test_hook.py test_ledger.py test_ledger_investigation.py test_setup.py -v
```

`test_hook.py` covers the governance hook, `test_ledger.py` the ledger hook,
`test_ledger_investigation.py` the investigation CLI and `test_setup.py` setup,
the settings loader, the MCP entry and the two skills. From the repo root, `make test-codex` runs all four.
