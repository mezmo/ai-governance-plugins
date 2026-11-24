---
name: investigate
description: Join an incident investigation in the Mezmo ledger, so every tool call Claude makes is filed under the incident and other agents and people working on it can see what was done. Use when the user asks Claude to investigate, triage or fix an incident, alert or outage from PagerDuty, Jira or Slack, or to pick up an investigation someone else started.
argument-hint: "<PagerDuty incident, Jira key or Slack channel>"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" join*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" close*)
  - mcp__plugin_mezmo_mezmo__get_ledger_investigation
  - mcp__mezmo__get_ledger_investigation
---

# Investigate an incident

The Mezmo ledger records each tool call. Once this session joins an investigation,
every record carries the incident's key, and the ledger files it under the incident's
investigation, the same one any other agent of your Mezmo account working on the incident joins.
The investigation comes from the incident's key, so agents that start from the same key meet in the
same investigation.

Arguments: $ARGUMENTS

## 1. Work out the incident's key

The key is `<source>:<id>`, from the first of these the incident has:

| Source | Key |
| --- | --- |
| A PagerDuty incident | `pagerduty:<incident id>`, such as `pagerduty:Q34FCCWSTU9OI2` |
| A Jira issue | `jira:<issue key>`, such as `jira:OPS-123` |
| A Slack channel | `slack:<channel id>`, such as `slack:C0123456789` |

One incident can have all three. Starting from a Slack channel or a Jira issue, use your PagerDuty,
Slack and Jira tools to find its PagerDuty incident, such as a link in the channel or the issue,
and use that when there is one. Otherwise two agents derive different keys and never meet. If you
can't tell which incident the user means, ask.

## 2. Join

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" join --incident-key <key> --service <name>
```

It prints one line with the `action`, the `incident_key` and the `service`.
From here on, the ledger files each tool call under that investigation.

`--service` records the service the incident is about, so people reading the investigation see
it even when no tool call targets it. Take it from the incident, such as the PagerDuty incident's
service or its alert's source, and give the deployment or app name, not a display name: `checkout`,
not `Checkout Production`. It must be lowercase letters, digits and `-`. If you aren't sure which
service it is, leave `--service` out rather than guess.

Run it on its own, as the whole Bash call, with the key written out as above: no `cd` or other
command before it, and no shell variable such as `"$KEY"` for the key. The ledger records the join
only when the Bash call starts with this command and the command itself gives the key the line
shows.

## 3. Read the investigation before changing anything

Call the `get_ledger_investigation` MCP tool with the same `incident_key`. It shows what other
agents and people already ran, what they changed, and what is waiting for approval. Build on that
work: don't repeat a change someone already made, or undo one they are waiting on.

If no `get_ledger_investigation` tool is available, the Mezmo plugin has no service key: tell the
user to add it with `/plugin configure mezmo@mezmo`, then `/reload-plugins`, and go on without the
read only if they say to.

## 4. Work the incident

- **One command per Bash call.** No `;`, `&&` or `||` chains. The ledger records one entry per
  tool call, with the exit status of its last command. In the first real run, `ls -la; ls /nope`
  was recorded as one call that ran, and the failure was visible only in the output. Separate
  calls give each action its own entry, outcome and approval.
- **Stay on the incident's service and namespace.** Act only on the service and environment the
  incident names. If a command fails because its target isn't found, report that and stop. Don't
  search the cluster for another deployment with the same name and act on that one: it belongs to
  someone else. In the first real run, after a `NotFound` the agent twice went looking, and found
  a real deployment of the same name in another namespace, one approval away from rolling it back.

## 5. Close

When your part is done, when you hand off, or when the user stops the work, close the
investigation with the same key, and say where things stand. As with the join, run it on its own
as the whole Bash call, with the key written out:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" close --incident-key <key> --status <status> --summary "<summary>"
```

`--status` is where the incident stands now:

- `resolved`: fixed, and you checked that it stays fixed.
- `mitigated`: the impact stopped, but the cause or a follow-up is still open.
- `ongoing`: still a problem, and you are handing it off.
- `no_action`: nothing needed doing, such as a false alarm.

`--summary` is a few plain sentences for whoever reads the investigation next: what happened,
what you changed (the changes, not the lookups), and what's left, with who or what owns it. Write
it out in the command, in double quotes, with no double quotes or shell variables inside it, in
2,000 characters or fewer. Don't say `resolved` unless you verified the fix. Take any time you
write from the investigation's entries (`get_ledger_investigation`) or a command's output, in UTC,
never from memory, or leave times out. For example:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" close --incident-key pagerduty:Q12ABC --status mitigated --summary "The checkout pods were OOM-killed at their 512 MiB limit after a traffic spike. Raised the limit to 1 GiB and restarted them; no kills in the last 20 minutes. Why memory grew is still open, and the checkout team owns it."
```

Close without them and the CLI refuses, so the close isn't recorded.

Later tool calls in this session are no longer filed under the incident, unless the session was
started by the launcher, whose investigation lasts the whole run.
