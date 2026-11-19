---
name: mezmo-investigate
description: Join an incident investigation in the Mezmo ledger, so every tool call Codex makes is filed under the incident and other agents and people working on it can see what was done. Use when the user asks Codex to investigate, triage or fix an incident, alert or outage from PagerDuty, Jira or Slack, or to pick up an investigation someone else started.
---

# Investigate an incident

The Mezmo ledger records each tool call. Once this session joins an investigation, every record
carries the incident's key, and the ledger files it under the incident's investigation, the same
one any other agent of your Mezmo account working on the incident joins. The investigation comes
from the incident's key, so agents that start from the same key meet in the same investigation.

The investigation CLI is `ledger_investigation.py` in the plugin root, the folder two levels above
this file's folder: this file is `<plugin root>/skills/mezmo-investigate/SKILL.md`. Below,
`<cli>` stands for the CLI's absolute path. Write it out in full in each command, quoted. A shell
variable in its place is not recognized.

## 1. Work out the incident's key

The key is `<source>:<id>`, from the first of these the incident has:

| Source | Key |
| --- | --- |
| A PagerDuty incident | `pagerduty:<incident id>`, such as `pagerduty:Q34FCCWSTU9OI2` |
| A Jira issue | `jira:<issue key>`, such as `jira:OPS-123` |
| A Slack channel | `slack:<channel id>`, such as `slack:C0123456789` |

One incident can have all three. Starting from a Slack channel or a Jira issue, use your
PagerDuty, Slack and Jira tools to find its PagerDuty incident, such as a link in the channel or
the issue, and use that when there is one. Two agents that pick different keys get different
investigations and never see each other's work. If you can't tell which incident the user means,
ask.

## 2. Join

```bash
python3 "<cli>" join --incident-key <key> --service <name>
```

It prints one line with the `action`, the `incident_key` and the `service`. From here on, the
ledger files each tool call under that investigation. Join even when the session was started by
`ledger_investigation.py launch`.

`--service` is the service the incident is about, from the incident itself, such as the PagerDuty
incident's service or its alert's source. Give the deployment or app name, not a display name:
`checkout`, not `Checkout Production`. It must be lowercase letters, digits and `-`. If you aren't
sure which service it is, leave `--service` out rather than guess.

Run it on its own, as the whole shell call, with the key written out: no `cd` or other command
before it, and no shell variable such as `"$KEY"` for the key. The ledger records the join only
when the shell call starts with this command.

## 3. Read the investigation before changing anything

Call the `get_ledger_investigation` MCP tool with `incident_key` set to the key, and read what was
already done: what other agents and people ran, what they changed, and what is waiting for
approval. Build on that work: don't repeat a change someone already made, or undo one they are
waiting on. If the `mezmo` MCP server or the tool isn't available, tell the user to ask Codex to
set up Mezmo, and don't change anything until they have.

## 4. Work the incident

- **One command per shell call.** No `;`, `&&` or `||` chains. The ledger records one entry per
  tool call, so separate calls give each action its own entry, outcome and approval.
- **Stay on the incident's service and namespace.** Act only on the service and environment the
  incident names. If a command fails because its target isn't found, report that and stop. Don't
  search the cluster for another deployment with the same name and act on that one: it belongs to
  someone else.

## 5. Close

When the work is done, or the user stops it, close the investigation with the same key, on its
own as the whole shell call, with the key written out:

```bash
python3 "<cli>" close --incident-key <key>
```

Later tool calls in this session are no longer filed under the incident.
