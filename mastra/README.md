# Mastra + Mezmo Governance

Mastra agent hooks that ask Mezmo Governance before each tool runs. Denied or
failed checks skip the tool (`proceed: false`). After an approved tool finishes,
`afterToolCall` appends a local JSONL record.

Requires `@mastra/core` 1.49.0 or later (agent `hooks`). Tested against
`@mastra/core` 1.77.0: a type check against its `ToolHooks` and
`Agent.generate` options, plus tests that call the hooks directly. A live run
with `gpt-5-mini` through `generate` and `stream` passed: see
[examples/check.sh](../examples/) `mastra`. Official docs:
https://mastra.ai/docs/agents/using-tools

## Install

Node 22.18+. Copy `mezmo-governance.ts` and `client.ts` into one folder of
your Mastra project, for example
`cp mastra/mezmo-governance.ts mastra/client.ts src/mastra/governance/`. Keep
both files together: `mezmo-governance.ts` imports `./client.ts`. Set the
variables, then pass the hooks to the agent:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_TOKEN="<service account token>"
```

```typescript
import { Agent } from "@mastra/core/agent";
import { mezmoGovernanceHooks } from "./mezmo-governance.ts";

export const agent = new Agent({
  id: "support-agent",
  name: "support-agent",
  instructions: "Help users with their questions.",
  model: "openai/gpt-5-mini",
  tools: { /* your tools */ },
  hooks: mezmoGovernanceHooks,
});
```

The import keeps the `.ts` extension. With `tsc`, set
`allowImportingTsExtensions` (with `noEmit`), or drop the extension if your
bundler resolves it.

### Run-level hooks bypass the gate

Mastra merges run-level `hooks` from `.generate()` or `.stream()` over the
agent's hooks, key by key. A run-level `beforeToolCall` replaces the
governance hook for that run, and tools then run without asking Mezmo. To add
your own hook, compose it so the governance hook runs last and sees the final
input:

```typescript
import { mezmoGovernanceHooks } from "./mezmo-governance.ts";

await agent.generate(prompt, {
  hooks: {
    ...mezmoGovernanceHooks,
    async beforeToolCall(ctx) {
      const mine = await myBeforeToolCall(ctx); // may return { proceed: false, output }
      if (mine?.proceed === false) return mine;
      return mezmoGovernanceHooks.beforeToolCall(ctx);
    },
  },
});
```

Only a `proceed: false` from your hook may skip the governance hook. Any other
return value still goes on to Mezmo.

### Give every agent the hooks

The hooks belong to one `Agent`. A sub-agent that another agent calls runs its
tools under its own hooks, not its caller's. Pass `hooks: mezmoGovernanceHooks`
to every agent, sub-agents included.

### Calls the hooks never see

These run without asking Mezmo:

- Provider-executed tools, such as a model provider's built-in web search, and
  client tools with no `execute`: Mastra does not execute them, so no hook
  fires.
- A workflow step, or any other code, that calls a tool's `execute` directly:
  the hooks run only in an agent's tool loop.

Keep such tools away from agents that need the gate, or gate them in your own
code.

`createMezmoGovernanceHooks({ fetchImpl })` builds a fresh hook pair if you
need to inject a custom fetch in tests. It takes only that option: the URL and
key always come from the environment.

Do not use Mastra's `requireToolApproval` for this. That is Mastra's own
approval UI. These hooks talk to Mezmo.

## Verify it works

From the folder that holds `mezmo-governance.ts`, ask the hook about one call
(Node 22.18+):

```bash
node --input-type=module -e 'import { mezmoGovernanceHooks } from "./mezmo-governance.ts";
console.log(await mezmoGovernanceHooks.beforeToolCall({ toolName: "echo", input: { text: "hi" }, context: {} }));'
```

`undefined` means Mezmo approved the call. `{ proceed: false, output: … }`
means it was denied or could not be checked; `output` says which. To check
fail-closed, run it again with
`MEZMO_GOVERNANCE_URL=http://127.0.0.1:9/authorize`: the output must say
`could not authorize`. After an approved tool runs in your agent, read the
last audit line: `tail -1 ~/.mezmo/governance-audit.jsonl`.

## What the hooks send

Built-in tools use namespace `mastra`. Names of the form `mcp__<server>__<tool>`
split into that server and tool.

### Tool names for policy

Mastra has no built-in shell or file tools: `tool_name` is the id you gave
`createTool`, such as `weatherTool`, in namespace `mastra`. Write policy for
`mastra:<tool id>`.

`request_id` is `mastra:<session_id>:<toolCallId>#<digest>`.
`<digest>` hashes the tool and its arguments, so an edited call gets its own
decision ([identity](../docs/protocol.md#identity)). `session_id` is the memory
`threadId` when present, otherwise the workflow `runId`. Without either it is
`agentId` plus a random id made once per process, so an approval never carries
over to the next run. A
call without a `toolCallId` (outside an agent loop) gets a random id, so it
never reuses an earlier decision.

A deny returns:

```json
{
  "proceed": false,
  "output": "Mezmo Governance denied this exact tool call: …"
}
```

The tool does not execute. The agent sees that string as the tool result.

If Mezmo is down, JSON is malformed, or the deadline expires while the decision
is still pending, the hook also skips the tool.

## Troubleshooting

`doctor` does not check this library: its setup lives in app code. The
[root table](../README.md#troubleshooting) lists each Mezmo error and its fix.
`MEZMO_GOVERNANCE_URL is required` or `must be a non-empty string` means the
process that runs the agent lacks the variables.

## Tests

Node 22.18+ (type stripping):

```bash
node --test test/*.test.ts
```

From the repo root: `make test-mastra`.
