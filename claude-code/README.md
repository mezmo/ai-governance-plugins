# Mezmo plugin for Claude Code

This plugin puts Claude Code's tool calls under [Mezmo](https://www.mezmo.com) Governance. Before
Claude runs a tool, the plugin asks Mezmo Governance whether that exact call is allowed, and
blocks it if the policy says no.

It can also send Claude Code's metrics, events and traces to Mezmo's
[OpenTelemetry source](https://docs.mezmo.com/telemetry-pipelines/open-telemetry-source), and
connect Claude to [Mezmo MCP](https://docs.mezmo.com/docs/mezmo-mcp) so it can query your logs and
work on pipelines. All three are independent, so you can turn on any of them.

A small status display in the prompt footer tells you whether governance and telemetry are
working.

## Getting started

1. **Install** from inside Claude Code:

   ```text
   /plugin marketplace add mezmo/ai-governance-plugins
   /plugin install mezmo@mezmo
   ```

   Installing from inside Claude Code opens the plugin's options dialog, where you can enter the
   service account token and the Mezmo API URL (empty means `https://api.mezmo.com`).
   `/plugin configure mezmo@mezmo` reopens it. The shell's `claude plugin install` never prompts,
   so after it run step 2.
2. **Set up governance**: `/mezmo:setup governance`. It asks for the API URL and the service
   account token, checks both Mezmo Governance and the [ledger](#ledger) without recording
   anything, and saves them as plugin options. It also connects [Mezmo MCP](#mezmo-mcp) with the
   same token, so Claude can read the ledger. One token and one URL turn on all three. For an API
   URL other than production, it asks once for the MCP URL. On a Mac where the Codex plugin's
   setup already saved a Mezmo key, it offers to use that key instead of asking for one.
3. **Check it**: restart Claude Code (`claude --continue` keeps the conversation), since a session
   loads MCP servers only when it starts, and run `/mezmo:test`. It shows the ledger's last send,
   such as `Ledger: last send 16:14:40 UTC to api.mezmo.com, HTTP 202, 2 events`.
4. **Investigate**: ask Claude to investigate an incident. It joins the incident's
   [investigation](#investigations) and reads the ledger first, so it sees what other agents and
   people already did. `/mezmo:investigate <incident>` does the same by name.

## Fail behavior

Until the plugin has a key, it lets every call run and sends nothing to Governance;
`MEZMO_GOVERNANCE_REQUIRED=1` makes it deny instead (see [Governance](#governance)). With a key, a
governance hook that crashes or times out blocks the call on Claude Code 2.1.295 or later. Older
versions ignore the hook's `onFailure: "block"`, so the tool runs. `--safe-mode` and
`"disableAllHooks": true` turn the governance check off along with your other hooks.

Editors that run Claude Code over ACP, such as Zed, use the Claude CLI bundled with
`@agentclientprotocol/claude-agent-acp`. An older adapter bundles an older CLI: 0.85.1 bundles
2.1.286, so a crash there lets the tool run. Set `CLAUDE_CODE_EXECUTABLE` to your installed
`claude` in the agent's `env` (Zed: `agent_servers.claude-acp.env`). Verified live in Zed 1.24.2
with adapter 0.93.0 and that setting: `Bash` denied, `Read` approved.

## Install

You need Python 3.9 or later. The scripts only use the standard library.
You need Claude Code v2.1.295 or later (see [Fail behavior](#fail-behavior)).

Tested version: Claude Code 2.1.301 with this plugin, run live against
`tools/mock_governance.py`: `Bash` was denied and `Read` was approved, and a
hook crash blocked the call.

From inside Claude Code, as in [Getting started](#getting-started), or from a shell:

```bash
claude plugin marketplace add mezmo/ai-governance-plugins
```

```bash
claude plugin install mezmo@mezmo
```

The shell install never asks for the plugin's options, so run `/mezmo:setup governance` (or
`claude plugin configure mezmo@mezmo`) afterwards.

Or, from a clone of this repository, run `./mezmo-governance install claude-code`
(add `--dry-run` to see the change first). It removes the older
`mezmo-governance` plugin, which would check every call twice. It then
installs `mezmo` from the clone, unless a `mezmo` marketplace is already set
up. Keep the clone where it is: Claude Code loads the plugin from it.
`./mezmo-governance uninstall claude-code` undoes it. Moving between this
route and the GitHub marketplace deletes the saved key (see below). After you
update the clone, run `uninstall`, then `install`: Claude Code keeps its own
copy, and `install` leaves an existing one alone.

Then run `/mezmo:setup` in Claude Code and start a new session. Claude Code reads these settings
when a session starts, so the session you ran setup in carries on as before.

Removing the marketplace, or uninstalling the plugin, deletes its saved settings, including the
service account token. That includes moving the plugin to another marketplace.
Afterwards, run `/reload-plugins` or start a new session, then run `/mezmo:setup governance`
again. Telemetry settings live in `~/.claude/settings.json` and the MCP server in
`~/.claude.json`, so they aren't affected.

| Command | What it does |
| --- | --- |
| `/mezmo:setup [governance\|telemetry\|mcp\|all]` | Sets up governance, telemetry, MCP or all of them. Governance and telemetry can also go in a managed-settings file for your organization |
| `/mezmo:test` | Checks your keys and shows what's configured |
| `/mezmo:remove [governance\|telemetry\|mcp\|all]` | Undoes setup. Uninstalling the plugin doesn't |
| `/mezmo:investigate <incident>` | Joins the incident's investigation in the [ledger](#investigations), so other agents working on it see this session's tool calls |
| `/mezmo` | Shows this session's status right away, without a Claude turn |

To try the plugin for one session without installing it, run
`claude --plugin-dir /path/to/ai-governance-plugins/claude-code`.

## Verify

Setup checks your Claude version before asking for a token. The marketplace can install
the plugin on an older CLI, so installation alone does not mean it will work.
`governance_config.py preflight` explains whether you need to upgrade, install the plugin,
or fix Claude configuration access. An old CLI still needs an upgrade if you store the
token in settings. The repository installer reports the binary and version it checks.

For an end-to-end check after setup:

1. Start a new Claude session. Run `/mezmo:test`; it tests only configured signals.
   Use `/mezmo:test --signals traces` to select traces explicitly, `--all-signals` to
   deliberately test all three, or `--check-only` to probe without publishing records.
2. Find the printed `test.run_id`, `service.name=mezmo-claude-plugin`, and `mezmo.test=true`
   in the pipeline destination. The checker confirms endpoint acceptance, not arrival.
3. Ask Claude to run `printf 'mezmo-onboarding-test\n'` using Bash, then confirm its
   decision under the intended Governance agent. Read/Edit/Write are not governed.
4. Separately look for actual Claude spans under `service.name=claude-code`.

Keep prompt, response and tool-detail capture off for this test. Governance still sends
Bash/MCP arguments for authorization, independently of telemetry content capture.
Choose your account and have your administrator prepare the token, policy and pipeline
before testing. The checker does not set them up for you.

To check the setup from the repository root, run `./mezmo-governance doctor claude-code` and
`./mezmo-governance smoke claude-code`. `doctor` reads `MEZMO_GOVERNANCE_URL` and
`MEZMO_GOVERNANCE_TOKEN` from the shell, not the key that setup saved, so export them first.
It also checks that a key reaches the hook, since without one the plugin lets every call run,
and sends the [ledger](#ledger) an empty batch with a key it can read (from `settings.json` or the
shell), which records nothing.
That passes when `claude plugin configure` shows a saved key with governance on, or when
`settings.json` has a key or `MEZMO_GOVERNANCE_REQUIRED=1` under `env`. A key or that variable
only in your shell gets a warning: Claude Code started from the Dock or another app may not see it.
`smoke` uses its own mock.
`/mezmo:test` checks the saved key from inside Claude Code.

## Passing a token safely

Setup uses `--key-file` so the token is not included in the Bash call sent to Governance.
The shared helper `python3 scripts/mezmo_common.py create-key-file` creates an empty private
file (0600) in a private directory (0700). Fill it locally, or through Claude's Write tool,
then pass its path with `--key-file "<path>" --delete-key-file` to the setup/check script.
Never fill it with a Bash command containing the token. Use a fresh file for each command.

The scripts reject symlinks, hard links, shared permissions and files owned by another user.
Key files need macOS or Linux; on Windows the helper stops before a file is created.
The delete flag removes the file on success or action failure. After interruption or an
argument-parsing failure, use `python3 scripts/mezmo_common.py delete-key-file "<path>"`.
Without the delete flag, the file is left for its owner to manage. Managed-settings commands
using a key file require `--output`, so the resulting credentials are not printed.

File input does not erase earlier chat, secure every copy on disk, or hide credentials in
child-process arguments such as `claude mcp add`. Prefer filling the file yourself if the
token should stay out of the conversation. Existing `--key` and environment options remain
available, but do not put literal keys in governed Bash calls.

If reading the plugin options fails, `status` reports the plugin state as unknown while showing
readable settings. `check` can still validate a supplied or readable token. `remove` cleans
settings and reports incomplete removal if it cannot also clear the plugin token.

## Governance

With governance on, a `PreToolUse` hook sends each Bash and MCP tool call to Mezmo Governance
before it runs:

| Policy says | What happens |
| --- | --- |
| Allow | The tool runs |
| Deny | The tool is blocked, and Claude sees the policy's reason |
| Ask a person | The call waits while someone approves or denies it in Mezmo, for up to 590 seconds. If nobody decides, it's blocked |
| Nothing: the service is unreachable, or rejects the key | The tool is blocked |

Bash calls show up in Mezmo under namespace `claude_code`, one call per part of the command (see
[Bash commands](#bash-commands)). MCP calls show up under the server's name, such as `mezmo`, with
the tool's own arguments. Other built-in tools, such
as Read, Edit and Write, don't go to Mezmo. Neither do the MCP servers the Claude desktop app adds
for its own task list, panes and sessions (`ccd_turn`, `ccd_session` and the like).

Governance stays off until you give it a key. Until then the hook lets every call through and
sends nothing to Governance. Register an agent for Claude Code in Mezmo Governance, give it a
service account with Governance permissions, and attach a policy. Then run
`/mezmo:setup governance` with that service account's token, a standard Mezmo service key (see
[Connect to Mezmo](../README.md#connect-to-mezmo)). Setup checks the key without recording a
decision, then saves it as a sensitive plugin option. Claude Code keeps a sensitive option in the
macOS Keychain. If the Keychain refuses the write, and on Linux and Windows, it is in
`~/.claude/.credentials.json`, readable only by you. Either way it stays out of `settings.json`.

| Setting | Plugin option | Environment variable (wins) | Default |
| --- | --- | --- | --- |
| Mezmo API URL | `api_url` | `MEZMO_GOVERNANCE_URL` sets the full authorize URL | `https://api.mezmo.com`, so `https://api.mezmo.com/governance/workflow/authorize` |
| Service account token | `governance_access_key` | `MEZMO_GOVERNANCE_ACCESS_KEY` or `MEZMO_GOVERNANCE_TOKEN` | none, so governance and the ledger are off |
| Governance on | `governance_enabled` | | on |
| Required | | `MEZMO_GOVERNANCE_REQUIRED=1` | off |

Releases before 0.6.0 kept the full authorize URL in a `governance_url` option. The hooks no
longer read it: run `/mezmo:setup governance`, which moves it to `api_url`, dropping its path.

The service only accepts plain identifiers as argument names, so an MCP tool with an argument
such as `max-results` gets blocked, and the message says which argument.

### Bash commands

Claude often runs several commands in one Bash call, such as `cd /repo && git push origin main`.
The plugin splits the command where Claude Code splits it for its own permission rules (`&&`,
`||`, `;`, `|`, `|&`, `&` and new lines) and sends each part to Mezmo as its own call. Mezmo
decides every part and goes with the strictest answer, so an allowed `cd` can't carry a
`git push` through. A part that runs once another has finished (after `&&`, `||` or `;`) names it
in `dependencies`.

Each part carries fields a rule can match:

| Field | For `git push --force origin main` |
| --- | --- |
| `command` | `git push --force origin main` |
| `program` | `git` |
| `arg_1`, `arg_2`, … | `push`, `origin`, `main`: flags left out, and only the first 10 |
| `flag_<name>` | `flag_force`: `true` |

Flags are read the common ways, without knowing the tool:

| Written | Fields |
| --- | --- |
| `--force` | `flag_force`: `true` |
| `--force-with-lease=main` | `flag_force_with_lease`: `"main"` |
| `-f` | `flag_f`: `true` |
| `-rf` | `flag_r` and `flag_f`, each `true` |
| `-delete`, a one-dash word of 3 letters or more | `flag_d`, `flag_e`, `flag_l`, `flag_t` and `flag_delete`, since `find`, `go` and `java` take long options after one dash |
| `-n5` | `flag_n`: `"5"` |
| `-9` | `flag_9`: `true` |

So a policy that sends force pushes to a person needs a rule for each spelling: `program eq git`,
`arg_1 eq push` and `flag_force eq true`, then the same with `flag_f`, and with
`flag_force_with_lease`. Rules can't say "either". A push of `+main` forces without any flag.

The first part also carries `full_command`, the whole command as Claude wrote it, and the call's
`description`. The wrappers Claude Code skips (`timeout`, `time`, `nice`, `nohup`, `stdbuf`,
`command`, `builtin` and a bare `xargs`) are skipped here too, as are `env` and `exec` without an
option or assignment, so `timeout 60 npm test` has `program` `npm`.

Some wrappers change how a command runs, so they stay the `program`, and the command they run
follows as a part of its own, with no `dependencies` on the wrapper's part:

| Written | Parts |
| --- | --- |
| `sudo git push --force` | `program` `sudo`, then `program` `git` with `flag_force` |
| `doas -u deploy npm test` | `program` `doas`, then `program` `npm` |
| `env FOO=1 git push` | `program` `env`, then `program` `git` |
| `eval "git push; rm -rf x"` | `program` `eval`, then `git` and `rm`, from the string eval runs |

Both parts carry the wrapper's text in `command`, except the parts of what `eval` runs, which
carry their own. Since the strictest answer wins, a rule that allows `sudo` no longer passes
whatever `sudo` runs: allow those commands too. A rule that sends `git push` to a person now
catches `sudo git push`. `sudo` or `doas` with an option other than `-E`, `-H`, `-P`, `-S`, `-b`,
`-k`, `-n`, `-D`, `-T`, `-g`, `-p` or `-u` (`sudo -s`, `sudo -i`) sends only its own part.

A part the plugin can't fully read gets no `program`, `arg_` or `flag_` fields: one with a variable
(`$HOME`), a redirect to a file, a subshell, an assignment such as `PATH=/tmp ls`, or a shell
keyword. A rule that allows by `program` can't match it, so it falls to your other rules or the
policy's default. Write those allow rules with `eq` or `in`, because `neq` and `nin` also match a
part with no `program`. A command the plugin can't split at all (a `$(...)` substitution,
backticks, a heredoc or a backslash at the very end) goes as one call with only Claude's input, as
before. A backslash at the end of a line joins the two lines, as in bash, so `pu\` at the end of
one line and `sh` at the start of the next read as `push`.

These fields are a guardrail, not a boundary. `git -C . push`, `bash -c "…"`, a script or a
Makefile gets past a rule that sends `git push` to a person, and an option's value lands among the
arguments: `kubectl -n prod delete` has `arg_1` `prod`. So list what may run, such as
`program in [ls, cat, rg]` with an allow rule, and send everything else to a person.

### Auto mode

When Mezmo Governance allows a call, the hook doesn't approve it on Claude Code's behalf, so in
auto mode Claude Code's classifier still checks it. When the classifier blocks one of these
calls, the hook counts it and adds a `blocked_by_auto_mode` line with the classifier's reason to
the audit log, and `/mezmo` shows the count. Governance doesn't hear about these blocks. With
[ledger](#ledger) sending on, each one goes to the ledger service as `agent.tool_call.not_run`.

## Ledger

The ledger keeps a record of the tool calls Claude Code makes: Mezmo gets one record when each
Bash and MCP tool call is requested and one when it ends. It is on whenever the plugin has a
service account token, and sends to `<API URL>/v1/audit/events` with that token. The ledger
service takes the account from the token, so there is no account to set. Turn it off with the
`ledger_enabled` plugin option (`/plugin configure mezmo@mezmo`). It also works with governance
switched off.

The ledger has its own hook script, `scripts/ledger_hook.py`, which Claude Code runs beside the
governance hook, in parallel with it. When governance refuses a call, Claude Code runs no other
hook, so the governance hook reports that call to the ledger itself, with a 2 second timeout.

| Event | When |
| --- | --- |
| `agent.tool_call.failed` | The tool ran and failed, or was interrupted |
| `agent.tool_call.not_run` | Governance or auto mode blocked the call |
| `agent.tool_call.ran` | The tool ran and succeeded |
| `agent.tool_call.started` | Claude asked to run the tool |

A call you reject at Claude Code's permission prompt has only `started`, because Claude Code
runs no hook then.

Each record carries the session id, a call id shared by its start and end, and the tool's name.
`started` also carries the tool's arguments. `ran` and `failed` carry the result or error, the
exit status and how long the tool took. `not_run` carries why the call didn't run. Arguments and
results are cut at 4,096 characters and are not redacted.

Each record also carries your git email (`git config user.email`) when one is set, so the ledger
can show who ran the session.

Each record also names the service and environment the call touched, such as `checkout/prod`, or
`unknown`, and whether the call reads or writes. Both are worked out from the kubectl commands in a
Bash call or from an MCP tool's name and arguments, with `prod` when no namespace or environment
is given.

`/mezmo:test` shows whether the ledger is on and why, the URL it sends to, an empty-batch check
of the key (`POST []`, answered `202` with `{"accepted": 0}`, which records nothing) and how many
records wait in the retry file. It also shows the hook's last send, which the hook keeps in the
session state beside governance's last check: the time, the URL's host, the HTTP status or network
error, and the event count, never the key or the events. That proves the hooks send even when the
key is a sensitive plugin option that no script can read back. Anything but `202` is a failure,
and `401` means a wrong key or a key for another account. With no send yet, ask Claude to run a
harmless command such as `printf 'mezmo-onboarding-test\n'`.

### Overrides

Environment variables win over the plugin options, which win over the defaults:

| Variable | Purpose |
| --- | --- |
| `MEZMO_LEDGER_URL` | Ledger events URL. Defaults to the API URL plus `/v1/audit/events`. When set, the ledger is on even with `ledger_enabled` off, as from managed settings |
| `MEZMO_LEDGER_KEY` | Mezmo service key. Defaults to the governance key: `MEZMO_GOVERNANCE_ACCESS_KEY`, `MEZMO_GOVERNANCE_TOKEN`, then the plugin option |
| `MEZMO_LEDGER_INCIDENT_KEY` | Incident the session is working on, such as `pagerduty:<incident id>`. Honored only when the [launcher](#investigations) set it |
| `MEZMO_LEDGER_SPOOL` | Retry file. Defaults to `~/.mezmo/ledger-spool.jsonl` |

The key is sent as `Authorization: Token <key>`, or as given when it starts with `Token ` or
`Bearer `. Setting `MEZMO_LEDGER_KEY` doesn't turn governance on. A `MEZMO_LEDGER_URL` with no key
sends no `Authorization` header. The URL must use `https`, or `http` to this machine only.

The ledger never blocks a tool call or changes a governance decision, because its hook always
exits successfully. If the ledger is slow or unreachable, the ledger hook stops waiting after 3
seconds and the call goes ahead. Records that can't be sent go to the retry file and are resent on
later tool calls. So do records the ledger rejects one by one, and records it refuses with a 401,
which a fixed key will let through. Records it refuses outright, such as with a 400 or 403, are
dropped. The retry file keeps at most 200 records and drops the oldest past that, and records
saved for a different `MEZMO_LEDGER_URL` are discarded once you change it. A record is resent
only with the key it was saved with: each record names that key by a short fingerprint, never the
key itself, so another account's records wait for their own key. The retry file holds
the full records, including arguments and output, and is readable only by you. Delete it to
discard what's waiting.

### Investigations

Agents working on the same incident file their tool calls under one investigation, so each can
read what the others already did before it acts. Each record of a session in an investigation
carries the incident's key, `pagerduty:<incident id>`, `jira:<issue key>` or `slack:<channel id>`,
and the ledger service files it under the investigation it works out from that key and the
token's account, so every agent of the account that starts from the same key lands in the same one.

| Event | When |
| --- | --- |
| `agent.investigation.closed` | The session left the investigation |
| `agent.investigation.joined` | The session joined the investigation |
| `agent.investigation.opened` | The launcher started a command in the investigation |

`/mezmo:investigate <incident>` works out the incident's key, preferring the PagerDuty incident
when a Slack channel or Jira issue has one, then joins. It runs
`python3 scripts/ledger_investigation.py join --incident-key <key> --service <name>`, and the
ledger hook, seeing that command print its result line for the same key and service, puts the
session in the investigation and sends `joined`. `--service` is optional: the deployment or app
the incident is about, such as `checkout`, as a DNS label. `joined` carries it, so the
investigation names its service even when no tool call targets it. The hook takes only a Bash call that starts by running the CLI with a Python
interpreter, with the key written out, so `join` and `close` are each run on their own as the
whole call: one after a `cd ... &&`, or with a key passed through a shell variable such as
`"$KEY"`, is not recognized. Claude then reads the
investigation with the `get_ledger_investigation` tool of [Mezmo MCP](#mezmo-mcp) before changing
anything, runs one command per Bash call, and at the end runs `close`, which sends `closed` and
takes the session out of it. Both need ledger sending on.

To start an agent inside an investigation, such as from an alert, use the launcher:

```bash
python3 scripts/ledger_investigation.py launch --incident-key <key> [--service <name>] -- <command...>
```

It sends `opened` with `opened_by: "alert"` and the service when given, then runs the command with
`MEZMO_LEDGER_INCIDENT_KEY` set, so a `claude -p` it starts files every call under the
investigation. It also sets `MEZMO_LEDGER_LAUNCHED` to a marker tied to that key. The ledger hook
takes the incident key from the environment only with that marker, so a `MEZMO_LEDGER_INCIDENT_KEY`
set any other way, such as one left from testing, is ignored instead of filing every call outside an
investigation under it. The hook says so once per session, and `/mezmo:test` warns of it. A
session's own investigation, from `join`, wins over the environment either way. Its exit code is the command's. Plugin options reach only Claude Code's hooks, so
export `MEZMO_LEDGER_KEY` (or `MEZMO_GOVERNANCE_ACCESS_KEY`) in the shell you run it from, and
`MEZMO_LEDGER_URL` for an API other than production. Without a key it runs nothing.

### References

After a `mezmo_memory_brief` call or one of the Insights reads below succeeds, the ledger hook saves
a small reference to what Claude read in the session's state file, in
`~/.mezmo/governance/sessions/`. Every later tool call record of the session carries the list
as `references`, so the ledger can show what Claude knew when it acted. The read's own record
doesn't carry it: the next call's `started` does.

| Type | Saved from | Fields |
| --- | --- | --- |
| `insights` | One of the Insights reads, one per call | `id`: the first `template_id`, `edge_key` or `trace_id` in the result, in that order, else a hash of the tool's name and arguments. `read_at`: when it was read. `tool`: the tool's name |
| `memory` | `mezmo_memory_brief`, one per record and one per remediation | `record`: the record's id. `version`: its version |

The list is the session's, newest last, and keeps at most 20: a read of the same thing again moves
it to the end, and past 20 the oldest go. Joining or closing an investigation doesn't clear it.
Only these small objects are saved, never a tool's arguments or output.

The Insights reads are a fixed list of 15 tools: `find_degraded_service_edges`,
`find_log_patterns`, `find_metric_analysis_anomalies`, `get_log_dimension_changes`,
`get_log_overview`, `get_log_pattern_series`, `get_metric_analysis_overview`,
`get_metric_analysis_series_values`, `get_service_failure_rates`, `get_service_failures`,
`get_service_graph`, `get_trace_spans`, `list_failed_traces`, `list_metric_analysis_series` and
`list_service_latency_summaries`. They and `mezmo_memory_brief` are matched by the tool's name on
any MCP server, so another server's tool by one of these names is counted too, and a tool Mezmo
adds later is not recognized until the list is updated.

The state file is the one governance keeps for [`/mezmo`](#status-in-claude-code). With governance
off, the first reference creates it, and `/mezmo` then shows zero counts for the session.

## Telemetry

You need a Mezmo ingestion key, from Settings > API Keys in Mezmo, and a pipeline with an
OpenTelemetry source. Mezmo accepts data without that source but doesn't route it anywhere.

A plugin can't set environment variables for Claude Code, so `/mezmo:setup telemetry` writes them
into the `env` block of `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`). It backs
the file up first, and stops to ask if the file already sends OpenTelemetry somewhere else. With
every signal on, it writes:

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_TRACES_EXPORTER": "otlp",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://logs.mezmo.com/otel",
    "OTEL_EXPORTER_OTLP_HEADERS": "apikey=<your Mezmo ingestion key>"
  }
}
```

Prompt text, response text and tool details stay out unless you opt in during setup. You can also
copy the block above in by hand. It has to go in your user settings or managed settings, because
Claude Code ignores these variables in a repository's `.claude/settings.json`.

In Mezmo, filter on `service.name` = `claude-code`. Cost and tokens are in the
`claude_code.cost.usage` and `claude_code.token.usage` metrics and in `claude_code.api_request`
events. [Claude Code monitoring](https://code.claude.com/docs/en/monitoring-usage) lists everything
else Claude Code sends.

## Mezmo MCP

[Mezmo MCP](https://docs.mezmo.com/docs/mezmo-mcp) gives Claude tools to analyze your logs (root
causes, patterns, histograms, correlated timelines), look at traces and AI investigations, and
inspect and change Telemetry Pipelines. You need a Mezmo service key, from Settings > API Keys in
Mezmo. It's a different key from the ingestion key.

`/mezmo:setup governance` connects it with the governance token. `/mezmo:setup mcp` connects only
the MCP server, with any service key. Either checks the key against the MCP URL, then runs the
command Mezmo documents (inside the script; do not paste a literal key into a governed Bash call):

```bash
claude mcp add --scope user --transport http mezmo https://mcp.mezmo.com/mcp --header "Authorization: Bearer <service key>"
```

The MCP URL follows the API URL: `https://api.mezmo.com` uses `https://mcp.mezmo.com/mcp`. For any
other API URL, setup asks for the MCP URL (`--mcp-url` for `governance_config.py configure`, `--url`
for `mcp_config.py add`). The server is always named `mezmo`: `/mezmo:investigate` allows its
`mcp__mezmo__get_ledger_investigation` tool, and any other name asks mid-investigation. Setup
replaces a user-scope server called `mezmo`, never one from a project's `.mcp.json`.

The key is in that `claude mcp add` command's arguments while it runs. `claude mcp add-json -`
would read it from stdin, but Claude Code refuses that inside a Claude Code session, where setup
runs.

The server is available in every project, in new sessions, with tools named `mcp__mezmo__<tool>`.
Claude Code loads MCP servers when a session starts, so the session that ran setup doesn't have
them: restart Claude Code (`claude --continue` keeps the conversation) to use them. `/mezmo:test`
says so when the server is set up but this session has no `mezmo` tools.
Claude Code keeps the key as plain text in `~/.claude.json`, as with any server added this way.
Some tools change pipelines: they create, update, delete, pause and publish them. Claude Code asks
before each MCP tool call unless you allow it. With governance on, these calls also go through
Mezmo Governance under namespace `mezmo`, so a policy can send deletes and publishes to a person.

## Status in Claude Code

The plugin includes a [mod](https://code.claude.com/docs/en/plugins/mods/overview) that adds one
label at the right of the prompt footer, such as `Mezmo · Governance 14 ✓ 1 ✗ · Telemetry ✓`.
It only shows the parts you've set up:

| Part | Meaning |
| --- | --- |
| `Governance 14 ✓ 1 ✗` | This session's governance checks so far |
| `Governance ⏳ 1 waiting` | A tool call is waiting for a person to decide |
| `Telemetry ✓` | This session sends telemetry to Mezmo |
| `Telemetry: start a new session to begin exporting` | You set up telemetry after this session started |
| `Telemetry ✗ key rejected`, `Telemetry ✗ unreachable` | The telemetry key check it runs once per session failed |

While a call waits for a person, a band above the prompt shows which call it is, how long it has
waited and the policy's reason, and the time ticks up every second. When the person decides, a
toast tells you. `/mezmo` prints the same information as text, and also works in `claude -p`.

The mod needs Claude Code 2.1.287 or later. Governance doesn't depend on it: the Python hook does
the blocking, so it keeps working where an organization blocks user-installed mods. The hook keeps
each session's counts and pending decisions in `~/.mezmo/governance/sessions/` for the mod to
read. It never writes tool arguments there.

## Rolling out to an organization

Pick "My organization" in `/mezmo:setup`. It writes one managed-settings file with the governance,
ledger and telemetry variables (`MEZMO_GOVERNANCE_URL` and `MEZMO_LEDGER_URL`, both from the API
URL, and the token; managed settings can't fill in a plugin's options), plus `MEZMO_GOVERNANCE_REQUIRED=1` if you want every Bash and MCP tool
call blocked on machines where governance isn't set up. The file holds your keys, so treat it like a secret.
Deploy it one of three ways:

- As a drop-in in `managed-settings.d/`, under `/Library/Application Support/ClaudeCode/` on
  macOS, `/etc/claude-code/` on Linux and WSL, or `C:\Program Files\ClaudeCode\` on Windows.
- In [server-managed settings](https://code.claude.com/docs/en/server-managed-settings) in the
  claude.ai admin console. This is the only route that reaches cloud sessions.
- Through MDM.

Governance also needs the plugin installed on each machine. Add `extraKnownMarketplaces` and
`enabledPlugins` (`"mezmo@mezmo": true`) to the same file to handle that.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Tool calls blocked with `HTTP 401` | Mezmo Governance rejected the service account token. Run `/mezmo:setup governance` again |
| Blocked with `ENOAGENT`, `EAGENTINACTIVE` or `EPOLICYUNASSIGNED` | The key works, but no active agent with a policy is linked to it |
| Blocked with `approval still pending` | Nobody decided in time. Approve the call in Mezmo, and Claude's retry goes through |
| Every call blocked, and governance isn't set up | Your organization set `MEZMO_GOVERNANCE_REQUIRED=1`. Ask them for the service account token |
| Every call blocked with `HTTP 401` right after reinstalling the plugin | The running session still has the old plugin's settings. Run `/reload-plugins` or start a new session, then `/mezmo:setup governance` |
| Governance is off after reinstalling the plugin | Reinstalling deleted the saved service account token. Run `/mezmo:setup governance` again |
| `/mezmo:test` shows records waiting in the ledger's retry file | The ledger was unreachable or refused the token. They are resent on later tool calls once the check passes |
| Telemetry checks pass, but nothing shows up in Mezmo | Start a new session, make sure a pipeline has an OpenTelemetry source, and give metrics a minute |
| Telemetry setup reports conflicting settings | Your settings already send OpenTelemetry somewhere else. Claude Code only exports to one endpoint |
| `/mezmo:test` shows the ledger's last send failed with `HTTP 401` | The ledger rejected the token: a wrong key, or one for another account. Run `/mezmo:setup governance` with the right service account token |
| `get_ledger_investigation` isn't available right after setup | The session started before setup connected the MCP server. Restart Claude Code; `claude --continue` keeps the conversation |
| `/mezmo:test` warns that `MEZMO_LEDGER_INCIDENT_KEY` is ignored | It is set, but not by the [launcher](#investigations), so calls outside an investigation aren't filed under it. Remove it where status says it's set |
| `/mezmo:test` warns that `MEZMO_LEDGER_ACCOUNT` is set | No release since 0.6.0 reads it. Remove it |
| The `mezmo` MCP server fails to connect with `401` | Mezmo rejected the service key. Run `/mezmo:setup mcp` with a current key and replace the server |

To see Claude Code's own telemetry export log:

```bash
claude -p "hello" --debug-file /tmp/claude-otel.log && grep "3P telemetry" /tmp/claude-otel.log
```

## Development

```bash
python3 -m unittest discover -s tests
```

From the repo root: `make test-claude-code`. `make test-claude-code-mod` runs `claude plugin test`.

```bash
claude plugin test
```

```bash
claude plugin validate .claude-plugin/plugin.json
```

Validation also lists every event the mod hooks and every call it makes. To type-check the mod,
use the config Claude Code generates when it validates or tests the plugin:

```bash
npx -p typescript tsc -p .claude-plugin/types/tsconfig.json
```

`scripts/` holds the Python that runs the governance and ledger hooks, writes settings, adds the
MCP server and checks keys.
`skills/` has the slash commands, and `hooks/` has the mod and the hook registrations.
