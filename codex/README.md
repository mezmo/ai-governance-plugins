# Codex + Mezmo Governance

A Codex CLI command hook that asks Mezmo Governance before each tool runs. If
Mezmo denies the call, or the hook cannot get an answer, the hook denies and
Codex skips the tool. After an approved tool finishes, the same script appends
a local JSONL record. A second hook script can send a record of each tool
call to the Mezmo ledger service; it is off until you set it up
(see [Ledger](#ledger)).
The audit log keeps up to 2000 characters of each tool result, which can hold
secrets; the hook creates it readable by you alone (file 0600, folder 0700).

Official docs: https://developers.openai.com/codex/hooks

The script needs Python 3.9 or later and uses only the standard library.

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

1. Copy both hook scripts, the governance hook and the ledger hook, and the
   [investigation](#investigations) CLI:

```bash
mkdir -p ~/.codex/hooks
cp codex/mezmo_governance_hook.py codex/mezmo_ledger_hook.py codex/ledger_investigation.py ~/.codex/hooks/
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

3. Export the URL and the service account token. The token is a standard
   Mezmo service key, not a key of the agent; see
   [Connect to Mezmo](../README.md#connect-to-mezmo):

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

4. Start Codex, run `/hooks`, and trust all four Mezmo hook entries: the
   governance hook and the ledger hook, each for `PreToolUse` and for
   `PostToolUse`. Until you trust them, Codex skips them and every tool runs
   ungoverned. Do not wait for a warning: Codex 0.163.0 gave none for the
   native plugin. Trust them again after each edit to the entries. Then run
   `./mezmo-governance doctor codex`: it fails while the governance
   PreToolUse hook is untrusted. It does not check the ledger entries.

Hooks are on by default. `hooks = false` under `[features]` in
`~/.codex/config.toml` turns them all off.

Codex inherits the environment of the shell that launches it.

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

Then trust all four hooks in `/hooks`. Until you do, Codex skips them without a
warning and every tool runs ungoverned (0.163.0). Use the plugin or the file
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

## Verify

From the repository root, with the two variables exported:

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
record for the made-up call. Unset `MEZMO_LEDGER_URL` first to avoid that.

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

The ledger keeps a record of the tool calls Codex makes. It is off by default.
When it is on, Mezmo gets one record when each tool call is requested and one
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

Turn it on by setting `MEZMO_LEDGER_ACCOUNT` and `MEZMO_LEDGER_URL`:

| Variable | Purpose |
| --- | --- |
| `MEZMO_LEDGER_ACCOUNT` | Your Mezmo account id. Required |
| `MEZMO_LEDGER_INCIDENT_KEY` | Incident the session is working on, such as `pagerduty:<incident id>`. Optional |
| `MEZMO_LEDGER_INVESTIGATION_ID` | Investigation the session belongs to. Optional |
| `MEZMO_LEDGER_KEY` | Mezmo service key. Optional, defaults to `MEZMO_GOVERNANCE_TOKEN` |
| `MEZMO_LEDGER_SPOOL` | Retry file. Optional, defaults to `~/.mezmo/ledger-spool.jsonl` |
| `MEZMO_LEDGER_URL` | Ledger events URL. Required |

Without `MEZMO_LEDGER_KEY`, the governance token is sent to
`MEZMO_LEDGER_URL`, whatever host that is. The key is sent as
`Authorization: Token <key>`, or as given when it starts with `Token ` or
`Bearer `. The URL must use `https`, or `http` to this machine only, and hold
no user name or password. For Mezmo, use
`https://<Mezmo API host>/v1/audit/events`.

`MEZMO_LEDGER_ACCOUNT` must be the account the key belongs to. If it is not,
the service answers 400 and the records are dropped. Records it refuses with
a 403 are dropped too. Records it refuses with a 401 are kept for retry,
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
records saved for a different `MEZMO_LEDGER_URL`. If the two send to
different URLs, give one its own `MEZMO_LEDGER_SPOOL`.

### Governance refusals

When the governance hook denies a call or cannot authorize it, it sends
`not_run` itself. It does so only after it has printed the deny, so the
ledger cannot change or lose the decision, and it spends at most about 3
seconds on it. A record cut off at that limit is lost, and a slow send can
deliver a record twice. Nothing is recorded when the deadline watchdog ends
the hook, or for input the hook cannot read.

### Investigations

Agents working the same incident can share one investigation. Their records
carry the same `thread_id`, so a second agent can read what the first did,
with the `get_ledger_investigation` MCP tool, before it acts. The `thread_id`
is worked out from `MEZMO_LEDGER_ACCOUNT` and the incident key, so every
session on the account that uses the same key gets the same one.

| Event | When |
| --- | --- |
| `agent.investigation.closed` | The session ran `ledger_investigation.py close` |
| `agent.investigation.joined` | The session ran `ledger_investigation.py join` |
| `agent.investigation.opened` | `ledger_investigation.py launch` started a command for an alert |

Each carries the incident key and the `thread_id`. `joined` and `closed` also
carry the session id, and `opened` carries `opened_by: "alert"`.

An incident key is `<source>:<id>`, with source `jira`, `pagerduty` or
`slack`, such as `pagerduty:Q34FCCWSTU9OI2`. The CLI,
`ledger_investigation.py`, has four commands:

| Command | What it does |
| --- | --- |
| `close --incident-key <key>` | Prints one line, `{"action": "closed", "incident_key": ..., "thread_id": ...}` |
| `id --incident-key <key>` | Prints the key and its `thread_id`. Nothing is recorded |
| `join --incident-key <key>` | Prints one line, `{"action": "joined", "incident_key": ..., "thread_id": ...}` |
| `launch --incident-key <key> -- <command...>` | Sends `opened`, then runs the command in the investigation |

`id`, `join` and `close` need only `MEZMO_LEDGER_ACCOUNT` and send nothing
themselves. The ledger hook does the rest when a `Bash` call runs the CLI with
`join` or `close` and its output has that line. Codex gives the hook no exit
code, so the line is the sign the command worked.

- After a `join`, the hook writes the investigation to the
  [session state](#session-state) and then sends `joined`. Every later call of
  the session carries the `thread_id`, and so does the join call's own `ran`.
  If the state can't be written, it warns on stderr and sends no `joined`.
- After a `close`, it sends `closed` and clears the session's investigation,
  but only when it is the one closed.

The CLI is at `~/.codex/hooks/ledger_investigation.py` for the file install,
where `./mezmo-governance install codex` copies it, and under
`~/.codex/plugins/cache/mezmo/mezmo-governance/<version>/` for the plugin.

Codex has no skills, so the instructions go in `AGENTS.md`. Paste this block
there and put the CLI's path in place of `<cli>`:

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
2. Run `python3 <cli> join --incident-key <key>`. Do this even when the session
   was started by `ledger_investigation.py launch`.
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
   same key.
```

`launch` is for an alert that starts an agent, for example
`python3 ~/.codex/hooks/ledger_investigation.py launch --incident-key <key> -- codex exec "<prompt>"`.
It sends `opened` with `opened_by: "alert"`, then runs the command in its
place with `MEZMO_LEDGER_INCIDENT_KEY` and `MEZMO_LEDGER_INVESTIGATION_ID`
set, so `launch` exits with the command's exit code. It needs `MEZMO_LEDGER_ACCOUNT` and
`MEZMO_LEDGER_URL`, and `MEZMO_LEDGER_KEY` (or `MEZMO_GOVERNANCE_TOKEN`)
exported in the shell it runs from. Without a key the ledger refuses `opened`
with a 401, and it waits in the retry file, so it may arrive after `joined`.
An `opened` that can't be sent is saved for retry and the command still runs.

Codex may run hooks in a background process whose environment was fixed when
it started, so the two variables `launch` sets may never reach the ledger
hook. That is why a launched session still runs `join`: the session state
wins over the environment. When the variables do reach the hook, every call
of the session carries the investigation, even after `close`.

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
An investigation there wins over `MEZMO_LEDGER_INVESTIGATION_ID`. The ledger
hook writes the investigation there after an [investigation](#investigations)
`join` and clears it after a `close`, keeping whatever else the file holds.
The file is readable only by you (file 0600, folder 0700). Nothing else in
this plugin writes these files.
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

## Tests

```bash
python3 -m unittest test_hook.py test_ledger.py test_ledger_investigation.py -v
```

`test_hook.py` covers the governance hook, `test_ledger.py` the ledger hook
and `test_ledger_investigation.py` the investigation CLI. From the repo root,
`make test-codex` runs all three.
