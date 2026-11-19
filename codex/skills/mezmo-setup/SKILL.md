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

These commands write outside the workspace (`~/.mezmo` and Codex's `config.toml`) and use the
macOS Keychain or the Linux keyring, so run each one outside the sandbox, with the user's
approval.

**Never put the key in a command, and never ask for it in the chat.** The governance hook records
every shell command's arguments in Mezmo, so a key in a command would end up in the trail. The key
goes through a private file the user fills in their own editor (step 3). Don't read, print or
`cat` that file.

## 1. See what is saved

```bash
python3 "<plugin root>/mezmo_setup.py" --status
```

It prints the saved API URL, whether the ledger is on, whether a key is saved and where, and the
Codex MCP entry. It never prints the key. Exit 1 means nothing is saved yet.

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

If the command itself is blocked with "no Mezmo key", the Mezmo hooks were trusted before setup
ran, so governance denies every call. Ask the user to quit Codex, start it once with
`codex --disable hooks`, ask again to set up Mezmo, and then restart Codex normally.

## 6. Trust the hooks

Tell the user to start a new Codex session, run `/hooks`, and trust the four Mezmo hooks:

- "Checking Mezmo Governance" and "Recording the governed result"
- "Recording the call in the Mezmo Ledger" and "Recording the result in the Mezmo Ledger"

Until they are trusted, Codex skips them and every tool call runs ungoverned and unrecorded. After
each plugin update, Codex asks to trust them again.

## 7. Check it

Run `--status` again (step 1). If the workspace is a clone of the plugin's repository (it has a
`mezmo-governance` script at its root), also run `./mezmo-governance doctor codex` there: it checks
the hooks' trust, the key, the ledger and the MCP connection.

Then the user can ask Codex to investigate an incident: it reads the Mezmo ledger first.

To undo all of it: `python3 "<plugin root>/mezmo_setup.py" --remove` deletes the saved key, the
settings and the MCP entry setup wrote.
