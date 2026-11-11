---
name: investigate
description: Join an incident investigation in the Mezmo ledger, so every tool call Claude makes is filed under the incident and other agents and people working on it can see what was done. Use when the user asks Claude to investigate, triage or fix an incident, alert or outage from PagerDuty, Jira or Slack, or to pick up an investigation someone else started.
argument-hint: "<PagerDuty incident, Jira key or Slack channel>"
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" id*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" join*)
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" close*)
  - mcp__mezmo__get_ledger_investigation
---

# Investigate an incident

The Mezmo ledger records each Bash and MCP tool call. Once this session joins an investigation,
every record is filed under the incident's investigation, the same one any other agent working on
the incident joins. Its id comes from the incident's key, so agents that start from the same key
meet in the same investigation.

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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" join --incident-key <key>
```

It prints one line with the `incident_key` and the investigation's `thread_id`. From here on, the
ledger files each tool call under that investigation.

Run it on its own, as the whole Bash call, with the key written out as above: no `cd` or other
command before it, and no shell variable such as `"$KEY"` for the key. The ledger records the join
only when the Bash call starts with this command and the command itself gives the key the line
shows.

## 3. Read the investigation before changing anything

Call the `get_ledger_investigation` MCP tool with the same `incident_key`. It shows what other
agents and people already ran, what they changed, and what is waiting for approval. Build on that
work: don't repeat a change someone already made, or undo one they are waiting on.

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

When the work is done, or the user stops it, close the investigation with the same key. As with
the join, run it on its own as the whole Bash call, with the key written out:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger_investigation.py" close --incident-key <key>
```

Later tool calls in this session are no longer filed under the incident, unless the session was
started by the launcher, whose investigation lasts the whole run.
