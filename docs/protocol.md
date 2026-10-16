# Mezmo Governance plugin protocol

Every plugin in this repo asks Mezmo the same question before a tool runs: may
this exact call execute? Mezmo answers allow, deny, or waits for a person.

## Endpoints

Set `MEZMO_GOVERNANCE_URL` to the full authorize URL:

```text
https://api.mezmo.com/governance/workflow/authorize
```

| Method | URL | When |
| --- | --- | --- |
| `POST` | `$MEZMO_GOVERNANCE_URL` | Before the tool runs |
| `GET` | `$MEZMO_GOVERNANCE_URL?decision_id=<id>` | After a client timeout or HTTP 409 |

Post-tool hooks only append a local JSONL file. They do not call Mezmo.

## Authentication

Register an agent in Mezmo with a service account that has Governance
permissions, and copy that service account's token. Use a service account
made for Governance alone, with no other permissions. Plugins send:

```http
Authorization: Token <access_key>
```

That header identifies the agent and the policy attached to it. Do not send a
user session token in its place.

If the token already starts with `Token ` or `Bearer `, plugins send it
unchanged.

## Authorize request

```json
{
  "version": 1,
  "decision_id": "uuid-v5-of-request-id",
  "request_id": "codex:session-1:toolu_01ABC#65e76bbd6d1b83cc",
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

Immediate or held `POST` body:

```json
{ "approved": true }
```

```json
{ "approved": false, "reason": "Deleting a pod in staging requires a human decision." }
```

Status poll:

```json
{ "status": "pending", "reason": null }
```

```json
{ "status": "approved", "reason": "looked it over, safe to run" }
```

```json
{ "status": "denied", "reason": "shell is not allowed" }
```

If policy sends the call for approval, Mezmo answers the `POST` at once with
HTTP 207 and `{"approved": false, "reason": "..."}`. That `approved: false` is
not the decision: the call is waiting for a person. Polls answer HTTP 207 with
`{"approved": false, "reason": null}` while it waits, then HTTP 200 with
`{"approved": true|false, "reason": ...}` once someone decides (seen live on
2026-10-16; a denial by a person was not seen yet). Plugins also accept the
`status` poll bodies above. A second `POST` with the same `decision_id` returns
HTTP 409.

## Client wait strategy

1. `POST` the authorize URL with remaining time until the overall deadline.
2. HTTP 200 with `approved: true` or `approved: false` is the decision.
3. HTTP 207 or 409: poll `GET ?decision_id=` until a decision or the deadline.
4. Client or transport timeout on the `POST`: same poll loop. Mezmo may still
   be waiting for a person.
5. `GET` HTTP 207, or `pending`: sleep `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` (default 5)
   and retry.
6. `GET` HTTP 404: Mezmo has no record of the decision yet, for example while
   the `POST` is still in flight. Treat it as `pending`. Every adapter does
   this, and the deadline in rule 7 still ends the wait.
7. Deadline reached while still pending: deny with reason
   `Mezmo Governance: approval still pending`.
   Every plugin treats an answer that arrives after the deadline as a deny,
   and bounds the whole answer, not each read: the TypeScript clients abort
   the fetch, the LangChain, OpenAI Agents SDK and Claude Agent SDK Python
   clients wrap the request in `asyncio.wait_for`, and a watchdog thread in
   the Python hooks prints the deny and exits 2.
8. Any other HTTP status, missing `approved` boolean, invalid JSON, missing
   env, or transport error: deny. The tool does not run. On a 4xx, the deny
   reason adds Mezmo's `code` and `message`, each on one line and cut at 200
   characters, such as `Mezmo Governance returned HTTP 403 ENOAGENT: no agent
   is registered for these credentials`. Nothing else from the reply is shown.
9. No client follows a redirect, on the `POST` or the poll `GET`. A 3xx is
   a deny, so the token never goes to a second URL.

## Identity

`request_id` is `<agent>:<session_id>:<tool_call_id>#<digest>`. `<digest>` is
the first 16 hex characters of SHA-256 over the JSON array
`[tool_namespace, tool_name, arguments]`, with sorted keys and no spaces.
`decision_id` is `uuid5(NAMESPACE_URL, request_id)`.

A retry of the same call, with the same id and the same arguments, reuses the
id and polls instead of opening a second decision. A call that keeps its id
but changes the tool or an argument gets a new id, so HTTP 409 never hands an
earlier approval to different arguments. Each client computes the digest its
own way; Mezmo never needs to recompute it, and `decision_id` stays a UUID.

The table lists the pieces before `#<digest>`.

| Runtime | `request_id` pieces | `origin.kind` | Default `tool_namespace` |
| --- | --- | --- | --- |
| Codex | `codex`, `session_id`, `tool_use_id` (plus `turn_id` when present) | `codex_hook` | `codex`, or MCP server from `mcp__<server>__<tool>` |
| Claude Code | `claude_code`, `session_id`, `tool_use_id` (plus `turn_id` when present) | `claude_code_hook` | `claude_code`, or MCP server from `mcp__<server>__<tool>` |
| Cursor | `cursor`, `conversation_id`, `tool_use_id`, or a fresh `run-<uuid>` when the event has none | `cursor_hook` | `cursor`, `mcp_server_name`, or `<server>` from `mcp__<server>__<tool>` |
| Gemini CLI | `gemini_cli`, `session_id`, a fresh `run-<uuid>` (no call id) | `gemini_cli_hook` | `gemini_cli`, or `mcp_context.server_name` |
| GitHub Copilot | `copilot`, `sessionId`, a fresh `run-<uuid>` (no call id) | `copilot_hook` | `copilot` |
| Goose | `goose`, `session_id`, `tool_call_id` | `goose_hook` | `goose`, or `<extension>` from `<extension>__<tool>` |
| Pi | `pi`, session id, `toolCallId` | `pi_extension` | `pi` |
| OpenCode | `opencode`, `sessionID`, `callID`; on 2.x `<callID>#<random UUID>`, because Code Mode reuses one call id for many inner calls | `opencode_plugin` | `opencode` |
| LangChain | `langchain`, `thread_id`, turn id, or one random id per middleware instance; tool call id, or a random id when it is missing | `langchain_middleware` | `langchain` |
| OpenAI Agents SDK | `openai_agents`, `RunConfig.group_id`, the run's trace id, or one random id per `MezmoGovernance` instance; `call_id`, or a random id when it is missing | `openai_agents_guardrail` | `openai_agents`, the MCP server's `name`, or the SDK tool namespace |
| Mastra | `mastra`, `threadId` or workflow `runId`, `toolCallId` or a random id when it is missing | `mastra_hooks` | `mastra` |
| Claude Agent SDK | `claude_agent_sdk`, `session_id`, `tool_use_id` or a random id when it is missing | `claude_agent_sdk_hooks` | `claude_agent_sdk`, or MCP server from `mcp__<server>__<tool>` |

MCP tool names of the form `mcp__<server>__<tool>` split into namespace
`<server>` and name `<tool>`. Arguments are never rewritten.

## Environment

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MEZMO_GOVERNANCE_URL` | yes | — | Full authorize URL |
| `MEZMO_GOVERNANCE_TOKEN` | yes | — | Service account token (plugins prefix `Token `) |
| `MEZMO_GOVERNANCE_TIMEOUT_SECONDS` | no | `590` | Overall deadline, under the 600s hook timeout. Capped at 590; `nan`, `inf` and values ≤ 0 are refused |
| `MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS` | no | `5` | Status poll spacing, at least 1 second |
| `MEZMO_GOVERNANCE_AUDIT_LOG` | no | `~/.mezmo/governance-audit.jsonl` | Local post-tool log |

OpenCode also takes these names as plugin options in `opencode.json`; an
option wins over the variable. On 2.x options are the only way to reach the
background service that runs the tools.

Both seconds values must be finite numbers greater than zero. Any other value
makes the hook deny every call with an error that names the variable.

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

`decision_id` and `request_id` are `null` when the event carries no tool call
id, as with Cursor's shell and MCP events and every Gemini CLI and Copilot
event.

`status` is `failed` when the result looks like an error (`isError`, `is_error`,
or non-zero `exit_code` or `exitCode`), otherwise `observed`. A result sent as
JSON text, such as Cursor's `tool_output`, is parsed first. Gemini CLI also marks a
`tool_response` with an `error` field as failed, and Goose a `PostToolUseFailure`
event. Failures writing the file go to
stderr and do not block the agent.

Pi and OpenCode add `"arguments_changed": true` when the arguments the tool ran
with differ from the approved ones (another extension changed them after
approval; on OpenCode 2.x this includes a tool rename). OpenCode also reads a
failure from `metadata.exit`.

LangChain writes no JSONL file; it can emit an OpenTelemetry span instead.
The OpenAI Agents SDK guardrail writes none either.

## Optional item field

The LangChain middleware adds `items[].tool_call_intent` only when you pass an
`intent_provider`. Leave it unset unless your Mezmo deployment accepts it.

## How policy shows up

| Policy | Plugin sees | Tool runs? |
| --- | --- | --- |
| allow | `{ "approved": true }` | yes |
| deny | `{ "approved": false, "reason": "..." }` | no |
| needs a person | POST stays open, then `{ "approved": true\|false, "reason": "..." }` | only after approve |
