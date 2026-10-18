# Live testing

How to check, by hand, that a real agent calls the Mezmo hook and obeys its
answer. `make test` cannot show this; see [What they do not confirm](../README.md#what-they-do-not-confirm).
The steps below are the ones behind the live-run table in the README.

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
droid plugin marketplace list    # note the marketplace name
droid plugin install mezmo-governance@<marketplace> --scope user
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
   environment; a Dock launch does not have them.
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
instead. Unset `MEZMO_GOVERNANCE_HOOK` afterwards, or every call stays blocked.

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

Stop each mock with Ctrl-C. Remove a Droid plugin with
`droid plugin uninstall mezmo-governance@<marketplace>`, and delete the cloud
agent test repository.
