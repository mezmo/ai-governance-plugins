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

## Fail behavior

Until the plugin has a key, it lets every call run and sends nothing; `MEZMO_GOVERNANCE_REQUIRED=1`
makes it deny instead (see [Governance](#governance)). With a key, a governance hook that crashes
or times out blocks the call on Claude Code 2.1.295 or later. Older versions ignore the hook's
`onFailure: "block"`, so the tool runs. `--safe-mode` and `"disableAllHooks": true` turn the
governance check off along with your other hooks.

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

```bash
claude plugin marketplace add mezmo/ai-governance-plugins
```

```bash
claude plugin install mezmo@mezmo
```

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
governance service account token in the keychain. That includes moving the plugin to another marketplace.
Afterwards, run `/reload-plugins` or start a new session, then run `/mezmo:setup governance`
again. Telemetry settings live in `~/.claude/settings.json` and the MCP server in
`~/.claude.json`, so they aren't affected.

| Command | What it does |
| --- | --- |
| `/mezmo:setup [governance\|telemetry\|mcp\|all]` | Sets up governance, telemetry, MCP or all of them. Governance and telemetry can also go in a managed-settings file for your organization |
| `/mezmo:test` | Checks your keys and shows what's configured |
| `/mezmo:remove [governance\|telemetry\|mcp\|all]` | Undoes setup. Uninstalling the plugin doesn't |
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
It also checks that a key reaches the hook, since without one the plugin lets every call run.
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
The delete flag removes the file on success or action failure. After interruption or an
argument-parsing failure, use `python3 scripts/mezmo_common.py delete-key-file "<path>"`.
Without the delete flag, the file is left for its owner to manage. Managed-settings commands
using a key file require `--output`, so the resulting credentials are not printed.

File input does not erase earlier chat, secure every copy on disk, or hide credentials in
child-process arguments such as `claude mcp add`. Prefer filling the file yourself if the
token should stay out of the conversation. Existing `--key` and environment options remain
available, but do not put literal keys in governed Bash calls.

If plugin/keychain access fails, `status` reports the plugin state as unknown while showing
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
sends nothing. Register an agent for Claude Code in Mezmo Governance, give it a service account
with Governance permissions, and attach a policy. Then run `/mezmo:setup governance` with that
service account's token, a standard Mezmo service key (see
[Connect to Mezmo](../README.md#connect-to-mezmo)). Setup checks the key without recording a
decision, then saves it as a plugin option, so it lives in the system keychain rather than in a
settings file.

| Setting | Plugin option | Environment variable (wins) | Default |
| --- | --- | --- | --- |
| Authorize URL | `governance_url` | `MEZMO_GOVERNANCE_URL` | `https://api.mezmo.com/governance/workflow/authorize` |
| Service account token | `governance_access_key` | `MEZMO_GOVERNANCE_ACCESS_KEY` or `MEZMO_GOVERNANCE_TOKEN` | none, so governance is off |
| Required | | `MEZMO_GOVERNANCE_REQUIRED=1` | off |

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
`command`, `builtin` and a bare `xargs`) are skipped here too, so `timeout 60 npm test` has
`program` `npm`.

A part the plugin can't fully read gets no `program`, `arg_` or `flag_` fields: one with a variable
(`$HOME`), a redirect to a file, a subshell, an assignment such as `PATH=/tmp ls`, or a shell
keyword. A rule that allows by `program` can't match it, so it falls to your other rules or the
policy's default. Write those allow rules with `eq` or `in`, because `neq` and `nin` also match a
part with no `program`. A command the plugin can't split at all (a `$(...)` substitution,
backticks or a heredoc) goes as one call with only Claude's input, as before.

These fields are a guardrail, not a boundary. `git -C . push`, `bash -c "…"`, a script or a
Makefile gets past a rule that sends `git push` to a person, and an option's value lands among the
arguments: `kubectl -n prod delete` has `arg_1` `prod`. So list what may run, such as
`program in [ls, cat, rg]` with an allow rule, and send everything else to a person.

### Auto mode

When Mezmo Governance allows a call, the hook doesn't approve it on Claude Code's behalf, so in
auto mode Claude Code's classifier still checks it. When the classifier blocks one of these
calls, the hook counts it and adds a `blocked_by_auto_mode` line with the classifier's reason to
the audit log, and `/mezmo` shows the count. Mezmo itself doesn't hear about these blocks yet.

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

`/mezmo:setup mcp` checks the key against `https://mcp.mezmo.com/mcp`, then runs the command
Mezmo documents (inside the script; do not paste a literal key into a governed Bash call):

```bash
claude mcp add --scope user --transport http mezmo https://mcp.mezmo.com/mcp --header "Authorization: Bearer <service key>"
```

The server is available in every project, in new sessions, with tools named `mcp__mezmo__<tool>`.
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

Pick "My organization" in `/mezmo:setup`. It writes one managed-settings file with the governance
and telemetry variables, plus `MEZMO_GOVERNANCE_REQUIRED=1` if you want every Bash and MCP tool
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
| Telemetry checks pass, but nothing shows up in Mezmo | Start a new session, make sure a pipeline has an OpenTelemetry source, and give metrics a minute |
| Telemetry setup reports conflicting settings | Your settings already send OpenTelemetry somewhere else. Claude Code only exports to one endpoint |
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

`scripts/` holds the Python that runs the governance hook, writes settings, adds the MCP server and
checks keys.
`skills/` has the slash commands, and `hooks/` has the mod and the governance hook registration.
