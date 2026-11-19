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

Steps 1 and 3 only read, or write a temporary file, so run them as usual, inside the sandbox.

Step 5 saves the setup outside the workspace (`~/.mezmo`, Codex's `config.toml`, and the macOS
Keychain or the Linux keyring) and checks the key over the network, which Codex's sandbox does not
allow. **Run the step 5 command outside the sandbox: call the shell tool with
`sandbox_permissions` set to `"require_escalated"`**, and a `justification` such as "Save the Mezmo
key and settings outside the workspace?". Codex then asks the user once to approve it. Don't run it
inside the sandbox first to see whether it fails.

If you can't run it outside the sandbox (the approval policy is `never`, the user declines, or
Codex rejects the request), use [the terminal route](#the-terminal-route) instead. With the
approval policy `never` you know that from the start: skip steps 3 and 5.

**Never put the key in a command, and never ask for it in the chat.** The governance hook records
every shell command's arguments in Mezmo, so a key in a command would end up in the trail. The key
goes through a private file the user fills in their own editor (step 3). Don't read, print or
`cat` that file.

## 1. See what is saved

```bash
python3 "<plugin root>/mezmo_setup.py" --status
```

It prints the saved API URL, whether the ledger is on, whether a key is saved and where, and the
Codex MCP entry. It never prints the key. Exit 1 means nothing is saved yet, not that the sandbox
stopped it: don't run it again outside the sandbox.

If the Codex MCP line says `(not written by setup)`, Codex already has a server named `mezmo` that
someone added by hand. Ask now whether setup may replace it (it backs up `config.toml` first). If
yes, add `--replace-mcp` to the command in step 5. If no, setup stops with exit 4 and saves nothing.

## 2. Ask for the Mezmo API URL

- **Production** (the default, unless `--status` shows another saved URL): `https://api.mezmo.com`.
  Its MCP server is `https://mcp.mezmo.com/mcp`, so ask nothing more.
- **Another Mezmo API URL**: then also ask for that environment's **Mezmo MCP URL**. It has no
  default. If the user doesn't know it, setup can run without it and skips the MCP; run setup
  again later with the MCP URL to add it.

## 3. The key

The key is a standard Mezmo service key of a service account with Governance permissions, the
account an administrator registered for Codex as an agent in Mezmo, with a policy attached. It is
not a key of the agent.

If `--status` shows a key is already saved, offer to keep it. If the user says yes, use
`--keep-key` in step 5 and skip the rest of this step.

Otherwise, create a private file for it:

```bash
python3 "<plugin root>/mezmo_key_file.py" create-key-file
```

It prints only the path of a new, empty file, mode 0600, in a private 0700 folder. Give the user
that path, and ask them to open it in their own editor, paste the key, save it, and tell you when
they're done. Don't open, read or write the file yourself.

## 4. Say what it turns on

Before saving, tell the user:

- Governance sends each tool call's arguments to Mezmo. Every tool call waits for Mezmo Governance
  before it runs. A call the policy denies is blocked with the policy's reason. A call the policy
  sends to a person waits for their decision, up to about 10 minutes, and is blocked if nobody
  decides. If Mezmo Governance can't be reached, calls are blocked.
- The Mezmo ledger records every tool call, with its arguments and output, under the key's
  account. `--no-ledger` turns it off.
- The Mezmo MCP server is added as `mezmo` with only its two ledger read tools turned on, and
  those run without asking. Its other tools, including the ones that change pipelines, stay off.
  Codex reads the key from where setup saved it each time it connects; `config.toml` never holds
  the key.

## 5. Save

Outside the sandbox (`sandbox_permissions: "require_escalated"`, see
[Running the commands](#running-the-commands)):

```bash
python3 "<plugin root>/mezmo_setup.py" --api-url <API URL> --mcp-url <MCP URL> --key-file "<KEY_FILE>" --delete-key-file
```

- Leave out `--mcp-url` for production, or when the user has no MCP URL.
- With a saved key, use `--keep-key` in place of `--key-file "<KEY_FILE>" --delete-key-file`.
- Add `--no-ledger` if the user wants the ledger off, and `--replace-mcp` if they agreed in step 1.

Setup checks the key with governance (a lookup that records no decision) and with the ledger (an
empty batch, which records nothing). Nothing is saved unless both pass. It saves the key in the
macOS Keychain, the Linux keyring, or else a 0600 file, and deletes the key file, on failure too.

- **0**: done. Report what it printed, without paths.
- **4**: Codex already has a `mezmo` MCP server that setup didn't write. Nothing was saved. Ask
  whether to replace it; if yes, run again with `--replace-mcp`, with a fresh key file from step 3.
- **1**: show the message. If it says the key was refused, ask for a fresh key from the service
  account in Mezmo, with a fresh key file from step 3. If it says the MCP wasn't set up,
  governance and the ledger are saved anyway.

## The terminal route

When step 5 can't run outside the sandbox, the user runs setup in their own terminal. Check that
the setup command exists, inside the sandbox:

```bash
test -x ~/.mezmo/bin/mezmo-setup && echo ok
```

If it prints `ok`, ask the user to run this in a terminal, outside Codex, adding `--no-ledger` if
they chose to keep the ledger off, and `--replace-mcp` if they agreed to replace an existing `mezmo`
MCP server:

```bash
~/.mezmo/bin/mezmo-setup
```

If it doesn't exist yet, give them `python3 "<plugin root>/mezmo_setup.py"` with `<plugin root>`
written out, plus `--no-ledger` if they chose the ledger off and `--replace-mcp` if they agreed to
replace an existing `mezmo` MCP server: the one time you show the path. Once it saves, `~/.mezmo/bin/mezmo-setup` exists for
next time.

It asks for the Mezmo API URL (Enter keeps production), the MCP URL for another API URL, and the
key, which it reads without showing it. The key file from step 3 isn't needed then: delete it with
`python3 "<plugin root>/mezmo_key_file.py" delete-key-file "<KEY_FILE>"` if you made one. Ask the
user to tell you when it is done, then go on with step 6.

If every call is blocked with `MEZMO_GOVERNANCE_REQUIRED=1`, the user's organization requires
governance, so no call runs until setup is done: only the terminal route works.

Until setup is done, Codex may warn "Mezmo Governance is off until you set up Mezmo". That is
expected: with no key, the hooks let every call run and record nothing.

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

Run `--status` again (step 1). If the workspace is a clone of the plugin's repository (it has a
`mezmo-governance` script at its root), also run `./mezmo-governance doctor codex` there: it checks
the hooks' trust, the key, the ledger and the MCP connection.

Then the user can ask Codex to investigate an incident: it reads the Mezmo ledger first.

To undo all of it: `python3 "<plugin root>/mezmo_setup.py" --remove` deletes the saved key, the
settings and the MCP entry setup wrote.
