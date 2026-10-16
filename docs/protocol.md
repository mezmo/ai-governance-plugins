# Mezmo Governance plugin protocol

Every plugin in this repo asks Mezmo the same question before a tool runs: may
this exact call execute? Mezmo answers allow, deny, or waits for a person.

## Endpoints

Set `MEZMO_GOVERNANCE_URL` to the full authorize URL:

```text
https://api.mezmo.com/governance/workflow/authorize
```

Development uses `https://api.use.dev.mezmo.it`. The path is the same.

| Method | URL | When |
| --- | --- | --- |
| `POST` | `$MEZMO_GOVERNANCE_URL` | Before the tool runs |
| `GET` | `$MEZMO_GOVERNANCE_URL?decision_id=<id>` | After HTTP 207, HTTP 409, or a client timeout |

Post-tool hooks only append a local JSONL file. They do not call Mezmo.

## Authentication

Register an agent in Mezmo and copy its access key. Plugins send:

```http
Authorization: Token <access_key>
```

That header identifies the agent and the policy attached to it. Do not send a
user session token in its place.

If the access key already starts with `Token ` or `Bearer `, plugins send it
unchanged.

## Authorize request

```json
{
  "version": 1,
  "decision_id": "uuid-v5-of-request-id",
  "request_id": "codex:session-1:toolu_01ABC",
  "scope": {
    "kind": "single",
    "session_id": "session-1"
  },
  "origin": {
    "kind": "codex_hook",
    "reason": "PreToolUse"
  },
  "items": [
    {
      "id": 0,
      "tool_namespace": "kubernetes",
      "tool_name": "pods_delete",
      "arguments": { "namespace": "staging", "name": "demo-pod" }
    }
  ]
}
```

Rules:

- Top-level fields are `version`, `decision_id`, `request_id`, `scope`,
  `origin`, `items`. Extra fields are rejected.
- `scope.kind` is `single` for these plugins. Optional `session_id` is the
  runtime's session, conversation, or thread id.
- `origin.kind` names the plugin. `origin.reason` is the event name. Optional
  `matched_pattern` is included when the runtime supplies a matcher.
- Each item needs `tool_name`. Plugins always send `tool_namespace`. If you omit
  it, Mezmo treats it as `"default"`. `arguments` is the JSON object the tool will
  receive. `id` is `0` for the single-item requests these plugins send.

## Authorize response

HTTP 200 `POST` body, the decision:

```json
{ "approved": true }
```

```json
{ "approved": false, "reason": "Deleting a pod in staging requires a human decision." }
```

If policy needs a person, the `POST` answers HTTP 207. Its body still says
`"approved": false`, but nothing has been decided yet. Poll the status URL until
someone approves or denies. A second `POST` with the same `decision_id` returns
HTTP 409.

The status poll answers with the same body as the `POST`, and the HTTP status
says whether it's final:

| `GET` status | Body | Meaning |
| --- | --- | --- |
| 207 | `{ "approved": false, "reason": null }` | Still waiting for a person. Poll again |
| 200 | `{ "approved": true, "reason": "looked it over, safe to run" }` | Approved |
| 200 | `{ "approved": false, "reason": "shell is not allowed" }` | Denied |
| 404 | `{ "code": "ENOENT" }` | The decision isn't visible yet. Poll again |

Plugins also accept an older poll body, `{ "status": "pending" | "approved" | "denied", "reason": ... }`.

## Client wait strategy

1. `POST` the authorize URL with remaining time until the overall deadline.
2. HTTP 200 with `approved: true` or `approved: false` is the decision.
3. HTTP 207: a person must decide. Ignore the body's `approved` and poll
   `GET ?decision_id=` until it answers HTTP 200 or the deadline passes.
4. HTTP 409: same poll loop.
5. Client or transport timeout on the `POST`: same poll loop. Mezmo may still
   be waiting for a person.
6. `GET` HTTP 207 or 404: sleep `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS`
   (default 5) and retry. HTTP 200 is the decision, read from `approved`.
7. Deadline reached while still pending: deny with reason
   `Mezmo Governance: approval still pending`.
8. Any other HTTP status, missing `approved` boolean, invalid JSON, missing
   env, or transport error: deny. The tool does not run.

## Identity

`request_id` is `<agent>:<session_id>:<tool_call_id>`. `decision_id` is
`uuid5(NAMESPACE_URL, request_id)` so a retry of the same call reuses the id
and polls instead of opening a second decision.

| Runtime | `request_id` pieces | `origin.kind` | Default `tool_namespace` |
| --- | --- | --- | --- |
| Codex | `codex`, `session_id`, `tool_use_id` (plus `turn_id` when present) | `codex_hook` | `codex`, or MCP server from `mcp__<server>__<tool>` |
| Claude Code | `claude_code`, `session_id`, `tool_use_id` | `claude_code_hook` | `claude_code`, or MCP server from `mcp__<server>__<tool>` |
| Cursor | `cursor`, `conversation_id`, `tool_use_id` | `cursor_hook` | `cursor`, or `mcp_server_name` / `MCP:<server>` |
| Pi | `pi`, session id, `toolCallId` | `pi_extension` | `pi` |
| OpenCode | `opencode`, `sessionID`, `callID` | `opencode_plugin` | `opencode` |
| LangChain | `langchain`, `thread_id` or turn id, tool call id | `langchain_middleware` | `langchain` |
| Mastra | `mastra`, `threadId` or workflow `runId`, `toolCallId` | `mastra_hooks` | `mastra` |

MCP tool names of the form `mcp__<server>__<tool>` split into namespace
`<server>` and name `<tool>`. Arguments are never rewritten.

## Environment

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | — | Full authorize URL |
| `MEZMO_GOVERNANCE_ACCESS_KEY` | yes | — | Agent access key (plugins prefix `Token `) |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590` | Overall deadline, under Codex/Claude's 600s hook default |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` | Status poll spacing |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` | Local post-tool log |

## Post-tool log

After an approved tool runs, observe hooks append one JSON object per line:

```json
{
  "ts": "2026-09-10T17:00:00+00:00",
  "agent": "codex",
  "decision_id": "...",
  "request_id": "...",
  "tool_namespace": "kubernetes",
  "tool_name": "pods_delete",
  "status": "observed",
  "result_preview": "{...}"
}
```

`status` is `failed` when the result looks like an error (`isError`, `is_error`,
or non-zero `exit_code`), otherwise `observed`. Failures writing the file go to
stderr and do not block the agent.

## How policy shows up

| Policy | Plugin sees | Tool runs? |
| --- | --- | --- |
| allow | `{ "approved": true }` | yes |
| deny | `{ "approved": false, "reason": "..." }` | no |
| needs a person | HTTP 207, then `GET` answers 207 until someone decides, then 200 with `approved` | only after approve |
