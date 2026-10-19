# Live testing

How to check, by hand, that a real agent calls the Mezmo hook and obeys its
answer. `make test` cannot show this. The steps below are the ones behind the
live-run table in [What they do not confirm](#what-they-do-not-confirm).

Each test asks the agent for one shell call and one file read. It passes when:

1. the shell call is blocked, the agent shows the hook's reason, and no
   `marker` file exists;
2. the read runs, and the agent prints `secret-word: pelican`;
3. the mock prints a `denied` line for the shell tool and an `approved` line
   for the read tool.

A file that exists after the run, or a mock that prints nothing, is a fail.

Read the mock before the agent's reply. A model can refuse a prompt without
calling any tool, and its reply then looks like a block. Copilot did this on
a prompt that asked for a "secret word", and the cloud agent did it on a task
that announced a hook test.

## 1. Set up once

Run these from the root of this repository.

1. Make a test folder with a file to read:

   ```bash
   T=$(mktemp -d); echo "secret-word: pelican" > "$T/note.txt"; echo "$T"
   ```

2. Start the mock in its own terminal. Deny each agent's shell tool:

   ```bash
   python3 tools/mock_governance.py --port 8787 \
     --deny Bash --deny Shell --deny shell --deny bash --deny Execute \
     --deny run_command --deny run_shell_command --deny run_in_terminal
   ```

   A pattern matches the tool name (`Execute`) or `<namespace>:<tool>`
   (`droid:Execute`), with shell wildcards, and is case-sensitive. The mock prints one line per call:
   `denied   droid:Execute`, `approved droid:Read`.

3. In the terminal that starts the agent, export the two lines the mock
   printed:

   ```bash
   export MEZMO_GOVERNANCE_URL=http://127.0.0.1:8787/governance/workflow/authorize
   export MEZMO_GOVERNANCE_TOKEN=local-test
   ```

4. Install the agent's hook and check it:

   ```bash
   ./mezmo-governance install <agent>
   ./mezmo-governance doctor <agent>
   ./mezmo-governance smoke <agent>
   ```

   `smoke` must print one denied and one approved line. It calls no model.
   For Claude Code, skip `install`, which governs every Claude Code session
   on the machine. Start `claude --plugin-dir ./claude-code` from this
   repository instead.

The two prompts used in every test:

```text
Run exactly this shell command: touch marker. Report whether it ran and quote any hook message.
Show me the contents of note.txt.
```

After the run, `ls "$T"` must show `note.txt` only.

## 2. Run each agent

Run each agent from `cd "$T"`, unless the step says otherwise. Agents not
listed here run the same two prompts in their normal session; each adapter
README has the install details.

### Droid

`droid exec` needs a paid Factory plan, or a model with your own key. For your
own Anthropic key, add this to `~/.factory/settings.json` and export
`CUSTOM_ANTHROPIC_API_KEY`:

```json
"customModels": [{"model": "claude-sonnet-5-5", "displayName": "Sonnet 5.5 BYOK",
  "baseUrl": "https://api.anthropic.com", "apiKey": "${CUSTOM_ANTHROPIC_API_KEY}",
  "provider": "anthropic", "maxOutputTokens": 16384}]
```

Then:

```bash
droid exec -m custom:Sonnet-5.5-BYOK-0 --auto high --cwd "$T" "<prompt>"
```

Without a plan or that model, Droid answers `402 No active subscription found`.

To test the plugin route instead of `install droid`, uninstall the file hook
first, then:

```bash
droid plugin marketplace add mezmo/ai-governance-plugins
droid plugin marketplace list    # shows ai-governance-plugins
droid plugin install mezmo-governance@ai-governance-plugins --scope user
```

### Windsurf (Devin Desktop)

Windsurf has no headless mode, so you type the prompts in Cascade.

1. Quit Windsurf (⌘Q).
2. Start it from the terminal that holds the exports:
   `devin-desktop "$T"`
3. Trust the folder. In Restricted Mode no hook loads.
4. Type the two prompts in Cascade.

To test a Dock launch, set the variables for the login session, quit
Windsurf, and start it from the Dock:

```bash
launchctl setenv MEZMO_GOVERNANCE_URL "$MEZMO_GOVERNANCE_URL"
launchctl setenv MEZMO_GOVERNANCE_TOKEN "$MEZMO_GOVERNANCE_TOKEN"
```

A Dock launch without them must block every call with
`MEZMO_GOVERNANCE_URL must be a non-empty string`. Remove them after the
test with `launchctl unsetenv`.

### Antigravity CLI

```bash
agy -p "<prompt>" < /dev/null
```

The mock lines read `antigravity:run_command` and `antigravity:view_file`.

### Copilot in VS Code

1. Install VS Code and the Copilot Chat extension:
   `brew install --cask visual-studio-code`, then
   `code --install-extension GitHub.copilot-chat`.
2. `./mezmo-governance install copilot`. VS Code reads `~/.copilot/hooks/`.
3. Quit VS Code (⌘Q), then start it from the terminal that holds the
   exports: `code "$T"`. The hook reads the settings from VS Code's
   environment. A Dock launch reads them from your login shell instead; see
   [Dock launch](#dock-launch).
4. Sign in to GitHub Copilot: the Accounts icon at the bottom left, then
   **Sign in to use GitHub Copilot**. Trust the folder.
5. Open Chat (⌃⌘I), select **New Chat** (`+`), then pick **Copilot** in the
   **Session Target** control before the first prompt. Type the two prompts.
   The mock logs `copilot:bash` and `copilot:view`.
6. Start a new chat with **Local** as the Session Target and type them again.
   It passes like step 5, with Local's tool names: the mock logs `denied
   copilot:run_in_terminal` and `approved copilot:read_file`. If a "Run bash
   command?" prompt appears, click Allow: the hook must still block it. With the
   earlier hook, `marker` got written and the mock logged nothing. See
   [copilot/](../copilot/README.md#vs-code).

### Cursor editor

1. `brew install --cask cursor`. Sign in on first start.
2. `./mezmo-governance install cursor`, or put the hook in the folder's
   `.cursor/hooks.json`, with the script path made absolute.
3. Quit Cursor (⌘Q), then start it from the terminal that holds the exports:
   `cursor "$T"`. Trust the folder.
4. Open the agent chat (⌘L), set the mode to **Agent**, and type the two
   prompts.

### Zed

Zed runs agents over ACP. Each agent loads its own hook, so the test is per
agent. Gemini CLI runs ungoverned there (see
[gemini-cli/](../gemini-cli/README.md#fail-behavior)): skip it. Start Zed
from the terminal that holds the exports: `zed "$T"`. Open the Agent panel
(⌘?), click **+**, and pick the agent.

Settings go in `~/.config/zed/settings.json`, under `agent_servers`. Settings
`env` wins over the shell's. To keep your own agent configs untouched, point
each agent at a scratch folder and install there:

```bash
S=$(mktemp -d)
CLAUDE_CONFIG_DIR="$S/claude" ./mezmo-governance install claude-code
CODEX_HOME="$S/codex" ./mezmo-governance install codex
CODEX_HOME="$S/codex" codex   # log in, trust both hooks in /hooks, then /quit
```

```jsonc
"agent_servers": {
  "claude-acp": { "type": "registry", "env": {
    "CLAUDE_CONFIG_DIR": "<S>/claude",
    "CLAUDE_CODE_EXECUTABLE": "/opt/homebrew/bin/claude" } },
  "codex-acp": { "type": "registry", "env": {
    "CODEX_HOME": "<S>/codex",
    "CODEX_PATH": "/opt/homebrew/bin/codex" } },
  "goose": { "type": "registry", "env": {
    "GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "gpt-5-mini" } }
}
```

- Claude Code with a scratch `CLAUDE_CONFIG_DIR` has no login: export
  `CLAUDE_CODE_OAUTH_TOKEN` in the shell that starts Zed. The mock lines
  read `claude_code:Bash` and `claude_code:Read`.
- Codex has no read tool: it reads with `cat`, so a `Bash` deny blocks the
  read too. For the approved call, ask it to create a file with
  `apply_patch`. The mock logs `approved codex:apply_patch`.
- Goose loads a plugin from the project, so it needs no install: copy
  `goose/` to `"$T/.agents/plugins/mezmo-governance"`. Goose needs a provider
  and its key, such as `OPENAI_API_KEY`, in the shell. The mock lines read
  `goose:shell` and `goose:read`.

### Dock launch

A GUI app started from the Dock reads no terminal exports. The Cursor editor,
VS Code and Zed read the variables from your login shell, so export them in
your shell profile. Windsurf reads only `launchctl setenv` (see
[Windsurf](#windsurf-devin-desktop)).

1. Export `MEZMO_GOVERNANCE_URL` and `MEZMO_GOVERNANCE_TOKEN` in your shell
   profile, such as `~/.zprofile`. Point them at the mock or at production.
2. Put the hook in the test folder, so nothing global changes: `.cursor/hooks.json`
   for Cursor, `.github/hooks/mezmo-governance.json` for VS Code, and
   `.agents/plugins/mezmo-governance/` for Goose in Zed. Use absolute script
   paths.
3. Quit the app (⌘Q), start it from the Dock or Spotlight, open `$T`, and
   type the shell prompt.
4. The shell call must be blocked with your policy's reason. A block that
   names a missing `MEZMO_GOVERNANCE_URL` means the app got no variables.

### Copilot cloud agent

The cloud agent runs on GitHub, so it needs a repository and production Mezmo.

1. Make a private test repository with a `README.md` that holds a secret word.
2. Commit `copilot/mezmo_governance_hook.py` as
   `.github/hooks/mezmo_governance_hook.py`, and this file as
   `.github/hooks/mezmo-governance.json`:

   ```json
   {"version": 1, "hooks": {
     "preToolUse": [{"type": "command", "timeoutSec": 600,
       "bash": "python3 .github/hooks/mezmo_governance_hook.py preToolUse"}],
     "postToolUse": [{"type": "command", "timeoutSec": 15,
       "bash": "python3 .github/hooks/mezmo_governance_hook.py postToolUse"}]}}
   ```

3. In **Settings > Copilot > Coding agent**, add `api.mezmo.com` to the
   firewall allowlist.
4. Assign Copilot a task that needs the shell, such as: "Add a file named
   build-info.txt with the output of `uname -a`. If a tool call is blocked,
   put the exact error text in the PR description."
5. Read the PR description, and the agent's log in the job's Actions run:
   `gh run view <run id> --log | grep tool.execution`. A run with no
   `tool.execution_start` line proves nothing.

Ask for a plain task, such as the one in step 4.

**Expected today: every tool call is blocked.** No tested route gets the
token to the hook, so the hook denies with
`MEZMO_GOVERNANCE_URL must be a non-empty string`, or names the token when
the URL is set inline in the `bash` command. That shows the hook runs and
fails closed, but no call reaches Mezmo. Tried on 2026-10-17, none of which
worked:

| Route | Result |
| --- | --- |
| Variable and secret in the repository's `copilot` environment | neither reaches the hook |
| `MEZMO_GOVERNANCE_URL=...` inline in the `bash` command | the URL arrives; the token is still missing |
| `.github/workflows/copilot-setup-steps.yml` writing both to `$GITHUB_ENV` | the URL reaches the hook, so `$GITHUB_ENV` works; the token does not, because the setup job gets no secret |

In the setup job, `${{ secrets.MEZMO_GOVERNANCE_TOKEN }}` was empty for a
secret in the `copilot` environment (also with `environment: copilot`) and
for a repository Actions secret, and no `MEZMO_GOVERNANCE_TOKEN` was in the
job's environment. The runs used a personal Copilot plan; an organization
plan was not tried. The setup-steps file must be on the default branch, and
its job must be named `copilot-setup-steps`. The workflow used:

```yaml
name: Copilot Setup Steps
on: workflow_dispatch
jobs:
  copilot-setup-steps:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Pass Mezmo Governance settings to the agent
        env:
          URL: ${{ vars.MEZMO_GOVERNANCE_URL }}
          TOKEN: ${{ secrets.MEZMO_GOVERNANCE_TOKEN }}
        run: |
          echo "MEZMO_GOVERNANCE_URL=$URL" >> "$GITHUB_ENV"
          [ -z "$TOKEN" ] || echo "MEZMO_GOVERNANCE_TOKEN=$TOKEN" >> "$GITHUB_ENV"
```

## 3. Hook crash

This checks that the agent blocks the tool when the hook itself fails. It
matters most for Codex, Gemini CLI, Droid, Windsurf and VS Code Local, which
run the tool when a hook exits 1: only the `|| exit 2` guard stops it.

```bash
printf 'raise RuntimeError("planted crash")\n' > "$T/../crash_hook.py"
export MEZMO_GOVERNANCE_HOOK="$(cd "$T/.." && pwd)/crash_hook.py"
```

Run the read prompt. The mock approves a read, so only the crash can block
it. The read must be blocked, and the mock must print no line for it. This
test covers only the agents whose example hook command reads
`MEZMO_GOVERNANCE_HOOK`: Codex, Cursor, Gemini CLI (`settings.example.json`),
Copilot, Droid, Windsurf and Antigravity CLI.
For Windsurf from the Dock, set it with `launchctl setenv MEZMO_GOVERNANCE_HOOK ...`
instead. For Goose, which runs `${PLUGIN_ROOT}/mezmo_governance_hook.py`, replace
that file in the plugin folder with the crash script, and restore it after. Unset `MEZMO_GOVERNANCE_HOOK` afterwards, or every call stays blocked.

## 4. Hook timeout

This checks that a decision nobody makes ends in a deny at the hook's own
deadline, before the agent's hook timeout. It takes about 10 minutes.

1. Start a second mock that holds the shell tool pending:

   ```bash
   python3 tools/mock_governance.py --port 8788 \
     --hold Bash --hold Shell --hold shell --hold bash --hold Execute \
     --hold run_command --hold run_shell_command --hold run_in_terminal
   ```

2. In the agent's terminal, run
   `export MEZMO_GOVERNANCE_URL=http://127.0.0.1:8788/governance/workflow/authorize`,
   then run the shell prompt.
3. After about 590 s, the call must be denied with `approval still pending`,
   and no `marker` file may exist. A run that ends on the agent's own timeout
   instead shows how that agent treats a timed-out hook.

Set `MEZMO_GOVERNANCE_TIMEOUT_SECONDS=20` for a quick run of the same path.

To see how the agent treats its own hook timeout, make a hook that sleeps
past a short timeout:

```bash
printf 'import sys, time\nsys.stdin.read()\ntime.sleep(60)\n' > "$T/../sleep_hook.py"
export MEZMO_GOVERNANCE_HOOK="$(cd "$T/.." && pwd)/sleep_hook.py"
```

Lower the pre-tool timeout to 5 in the installed config (`timeout`, or
`timeoutSec` for Copilot), then run the shell prompt. For Claude Code, copy
`claude-code/` and edit its `hooks/hooks.json`, then start `claude
--plugin-dir <copy>`. Codex needs the changed hook trusted again in `/hooks`.
Seen on 2026-10-18: Claude Code and Cursor CLI block the call. Copilot CLI and
Codex CLI run the tool, guard or not. Restore the timeout afterwards.

## 5. Production Mezmo

1. Use a service account token from Mezmo, as in the README's setup, and the
   production URL:

   ```bash
   export MEZMO_GOVERNANCE_URL=https://api.mezmo.com/governance/workflow/authorize
   export MEZMO_GOVERNANCE_TOKEN="<service account token>"
   ```

2. In a separate terminal, not the one that starts the agent, add a deny
   rule for the shell tool and an allow rule for the read tool. This needs a
   key with Governance policy rights:

   ```bash
   export MEZMO_GOVERNANCE_ADMIN_KEY="<admin key>"
   ./mezmo-governance policy apply <agent> --yes
   unset MEZMO_GOVERNANCE_ADMIN_KEY
   ```

   Without rules, the fallthrough decides, and a call may wait for approval.

3. `./mezmo-governance smoke --live <agent>` must say denied for the shell
   call and allowed for the read.
4. Run the two prompts in the agent. The shell call must show your rule's
   message, such as `Shell blocked by Mezmo policy`.

## 6. Clean up

```bash
./mezmo-governance uninstall <agent>
unset MEZMO_GOVERNANCE_HOOK MEZMO_GOVERNANCE_ADMIN_KEY MEZMO_GOVERNANCE_URL \
  MEZMO_GOVERNANCE_TOKEN MEZMO_GOVERNANCE_TIMEOUT_SECONDS
rm -rf "$T"
# macOS, only if you set them for a Dock launch:
launchctl unsetenv MEZMO_GOVERNANCE_URL; launchctl unsetenv MEZMO_GOVERNANCE_TOKEN
```

For Zed, remove the `env` blocks you added under `agent_servers` and delete the
scratch folder `$S`. It holds the Codex login.

Stop each mock with Ctrl-C. Remove a Droid plugin with
`droid plugin uninstall mezmo-governance@ai-governance-plugins --scope user`, and delete the cloud
agent test repository.

## What the tests confirm

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
  including the `|| exit 2` guard for Codex, Gemini CLI, Droid, Windsurf and
  Copilot (Copilot's example also carries a PowerShell form that exits 2 on a
  non-zero exit);
- `./mezmo-governance` installs each agent, finds it installed, changes
  nothing on a second install, and on uninstall leaves each config as it was,
  with the user's other entries and a backup; a run stopped between two steps
  is finished by the next one; `--dry-run` changes nothing; the key is in no
  file or output (`tools/test_mezmo_governance_cli.py`, with fake `claude`,
  `gemini` and `pi` binaries);
- the shared code is identical across the eight Python hook adapters, across the
  four `client.ts` copies, and between the LangChain Python client and its
  OpenAI Agents SDK and Claude Agent SDK copies; the Python hooks import on
  Python 3.9;
- the Claude Code hook matches the shared Python hook code in the definitions
  `CLAUDE_CODE_SHARED_DEFINITIONS` names (`codex/test_hook.py`).

## What they do not confirm

The tests cannot show that a runtime calls the hook, or how it treats the
answer. That was checked by hand, against `tools/mock_governance.py`, with
one shell call (expected: denied) and one file read (expected: approved):

| Agent | Version | Deny and allow | Hook crash |
| --- | --- | --- | --- |
| Claude Code | 2.1.301 with the Mezmo plugin (2.1.296 with the earlier hook) | pass | blocked, also on a timeout (2.1.301) |
| Codex CLI | 0.162.1 | pass | blocked with the guard; runs without it; runs on a timeout, guard or not (0.163.0) |
| Cursor CLI | 2026.10.14 | pass | blocked, also on a timeout |
| Cursor editor | 3.25.9, started from the Dock, project `.cursor/hooks.json`, against production Mezmo | pass: the hook read the settings from the login shell | not checked |
| Gemini CLI | 0.64.0 | pass | blocked with the guard; runs without it |
| Copilot CLI | 1.0.95, 1.0.98 | pass, with user-level hooks | blocked (1.0.95); runs on a timeout (1.0.98) |
| VS Code | 1.142.0 with Copilot Chat 0.70.0 | Copilot harness: pass. Local harness: pass (`run_in_terminal` denied, `read_file` approved); with the earlier hook it ran every tool. Started from the Dock with workspace `.github/hooks`, against production Mezmo: pass | not checked |
| Goose CLI | 1.54.0 | pass | blocked with `on_failure: block`, also on a timeout; runs without it |
| Claude Code in Zed | claude-agent-acp 0.93.0 in Zed 1.24.2, `CLAUDE_CODE_EXECUTABLE` set to Claude Code 2.1.301, Zed started from a shell | pass | not checked |
| Codex in Zed | codex-acp 2.2.2 in Zed 1.24.2, `CODEX_PATH` set to Codex 0.163.0, hooks trusted first in `codex` `/hooks`, Zed started from a shell | pass (`Bash` denied, `apply_patch` approved) | not checked |
| Goose in Zed | Goose 1.55.0 over ACP in Zed 1.24.2, plugin in the project's `.agents/plugins`; started from a shell, and from the Dock against production Mezmo | pass, both ways | blocked |
| Droid | 0.244.0, `droid exec` with a bring-your-own-key model (no Factory plan); also against production Mezmo | pass | blocked with the guard |
| Windsurf | 1.126.0 (Devin Desktop 3.10.67); also against production Mezmo | pass, started from a shell or from the Dock after `launchctl setenv`; a plain Dock launch passes no settings, so every call is denied | blocked with the guard |
| Antigravity CLI | 1.4.0, `agy -p` with a workspace `.agents/hooks.json`; 1.4.1 with `~/.gemini/config/hooks.json` against production Mezmo | pass | blocked, also on exit 1, output that is not JSON, a missing script and a timeout |
| OpenCode | 2.0.25 | pass, through the background service with plugin options | not checked |
| Pi | 1.3.0 | pass | not checked |
| Mastra | 1.77.0, `generate` and `stream` | pass | not checked |
| LangChain | 1.4.6, `invoke` and `ainvoke` | pass | not checked |
| OpenAI Agents SDK | 0.24.0, `run_sync`, `run_streamed` and `run` | pass | blocked, in the tests |
| Claude Agent SDK | Python 0.2.168, `query` and `ClaudeSDKClient`; TypeScript 0.3.301, `query` | pass | runs if a callback raises (Python, checked), so the callbacks catch every error |

The TypeScript plugins also pass `tsc --strict` against the real Pi, OpenCode
(1.19.0 and 2.0.25) and Mastra types. That check is not part of `make test`.

To repeat any of these checks, follow the sections above: the setup, the two
prompts, and the crash, timeout and production runs.

Not checked at all: a decision a person approves while the hook waits,
Windows, and how most runtimes behave when the hook times out. The
Copilot cloud agent ran the hook from `.github/hooks/` and blocked every call,
because the hook got neither the URL nor the token (see [copilot/](../copilot/README.md#cloud-agent)). For Mastra,
LangChain, the OpenAI Agents SDK and the Python Claude Agent SDK,
[examples/check.sh](../examples/) runs a real agent and fails if the gate lets
the wrong tool run.
