---
name: mezmo-setup
description: Set up, reconfigure or check Mezmo for Codex - Mezmo Governance on each tool call, the Mezmo ledger, and the Mezmo MCP server, all from one Mezmo API URL and one service key. Use when the user asks Codex to set up, configure, connect or check Mezmo, Mezmo Governance, the ledger, the Mezmo MCP or the Mezmo key.
---

# Set up Mezmo for Codex

One Mezmo service key and one Mezmo API URL turn on three things:

- **Governance**: before each tool call, a hook asks Mezmo Governance whether this exact call may
  run, and blocks it when the answer is no.
- **The ledger**: a second hook records each tool call, with its arguments and output, in the Mezmo
  ledger under the key's account.
- **The Mezmo MCP**: Codex connects to Mezmo's MCP server as `mezmo`, so an investigation can read
  the ledger (`get_ledger_investigation`, `list_ledger_entries`).

## Running the commands

The plugin root is the folder two levels above this file's folder: this file is
`<plugin root>/skills/mezmo-setup/SKILL.md`. Below, `<plugin root>` stands for that folder's
absolute path. Write it out in each command, quoted. Never show it, or any version number, to the
user: tell them what a command does, not where it is.

Run every command here as usual, inside the sandbox. **The user saves the setup themselves, in
their own terminal (step 4):** setup writes `~/.mezmo`, Codex's `config.toml` and the macOS
Keychain or the Linux keyring, and checks the key over the network. Codex's sandbox allows none of
that, and Codex can't reach the Keychain even with extra permissions. Don't try to run setup
yourself, inside or outside the sandbox.

**Never ask for the key in the chat, and never put it in a command.** The governance hook records
every shell command's arguments in Mezmo. Setup asks for the key itself, at a prompt that doesn't
show it.

## 1. See what is saved

```bash
python3 "<plugin root>/mezmo_setup.py" --status
```

It prints the saved API URL, whether the ledger is on, whether a key is saved and where, and the
Codex MCP entry. It never prints the key. Exit 1 means nothing is saved yet.

If the Codex MCP line says `(not written by setup)`, Codex already has a server named `mezmo` that
someone added by hand. Ask now whether setup may replace it (it backs up `config.toml` first). If
yes, add `--replace-mcp` to the command in step 4.

## 2. Say what it turns on

Tell the user, briefly:

- Governance sends each tool call's arguments to Mezmo. Every tool call waits for Mezmo Governance
  before it runs. A call the policy denies is blocked with the policy's reason. A call the policy
  sends to a person waits for their decision, up to about 10 minutes, and is blocked if nobody
  decides. If Mezmo Governance can't be reached, calls are blocked.
- The Mezmo ledger records every tool call, with its arguments and output, under the key's
  account. `--no-ledger` turns it off.
- The Mezmo MCP server is added as `mezmo` with only its two ledger read tools turned on, and
  those run without asking. Codex reads the key from where setup saved it each time it connects;
  `config.toml` never holds the key.

## 3. What they need

- **The Mezmo API URL**: production, `https://api.mezmo.com`, unless they use another Mezmo
  environment.
- **For another environment, its Mezmo MCP URL.** Without it, setup skips the MCP; they can run
  setup again later to add it.
- **The key**: a standard Mezmo service key of the service account an administrator registered for
  Codex as an agent in Mezmo, with a policy attached. It is not a key of the agent. If `--status`
  shows a key is already saved, setup can keep it: add `--keep-key`.

## 4. They run setup in their terminal

Check that the setup command exists, inside the sandbox:

```bash
test -x ~/.mezmo/bin/mezmo-setup && echo ok
```

If it prints `ok`, ask the user to run this in a terminal, outside Codex:

```bash
~/.mezmo/bin/mezmo-setup
```

If it doesn't exist yet, give them `python3 "<plugin root>/mezmo_setup.py"` with `<plugin root>`
written out: the one time you show the path. Once setup saves, `~/.mezmo/bin/mezmo-setup` exists
for next time.

Add `--no-ledger` if they want the ledger off, `--keep-key` to keep a saved key, and
`--replace-mcp` if they agreed in step 1. Add nothing else: the command asks for the rest.

Tell them what it asks, in order: the Mezmo API URL (Enter keeps production), the MCP URL (only for
another API URL; blank skips the MCP), and the key, which they paste at a prompt that shows nothing,
then press Enter. It checks the key with governance and the ledger, and saves nothing unless both
pass. Ask them to tell you when it's done, and what it printed if it failed.

If every call is blocked with `MEZMO_GOVERNANCE_REQUIRED=1`, the user's organization requires
governance, so no call runs until setup is done. The terminal command still works.

Until setup is done, Codex may warn "Mezmo Governance is off until you set up Mezmo". That is
expected: with no key, the hooks let every call run and record nothing.

## 5. Check what it saved

Run `--status` again (step 1). Confirm the API URL, that a key is saved, and the MCP entry. If the
key is missing, setup didn't finish: ask what it printed.

## 6. Start a new session and trust the hooks

If setup printed "Start a new Codex session to use the Mezmo MCP", say so: Codex connects to MCP
servers when a session starts, so this session doesn't have the Mezmo MCP's tools.

Tell the user to start a new Codex session, run `/hooks`, and trust the five Mezmo hooks:

- "Checking Mezmo Governance" and "Recording the governed result"
- "Recording the call in the Mezmo Ledger", "Recording the result in the Mezmo Ledger" and
  "Recording the turn's end in the Mezmo Ledger"

Until they are trusted, Codex skips them and every tool call runs ungoverned and unrecorded. After
each plugin update, Codex asks to trust them again.

## 7. Check it

If the workspace is a clone of the plugin's repository (it has a `mezmo-governance` script at its
root), run `./mezmo-governance doctor codex` there: it checks the hooks' trust, the key, the ledger
and the MCP connection.

Then the user can ask Codex to investigate an incident: it reads the Mezmo ledger first.

To undo all of it: `python3 "<plugin root>/mezmo_setup.py" --remove` deletes the saved key, the
settings and the MCP entry setup wrote.
