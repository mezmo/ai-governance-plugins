# Mastra + Mezmo Governance

Mastra agent hooks that ask Mezmo Governance before each tool runs. Denied or
failed checks skip the tool (`proceed: false`). After an approved tool finishes,
`afterToolCall` appends a local JSONL record.

Requires `@mastra/core` 1.49.0 or later (agent `hooks`). Official docs:
https://mastra.ai/docs/agents/using-tools

## Install

Copy the TypeScript files into your Mastra project and pass the hooks to the
agent:

```bash
export MEZMO_GOVERNANCE_URL="https://api.mezmo.com/governance/workflow/authorize"
export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>"
```

```typescript
import { Agent } from "@mastra/core/agent";
import { mezmoGovernanceHooks } from "./mezmo-governance.ts";

export const agent = new Agent({
  id: "support-agent",
  name: "support-agent",
  instructions: "Help users with their questions.",
  model: "openai/gpt-5.6-sol",
  tools: { /* your tools */ },
  hooks: mezmoGovernanceHooks,
});
```

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
      return mine ?? mezmoGovernanceHooks.beforeToolCall(ctx);
    },
  },
});
```

`createMezmoGovernanceHooks()` builds a fresh hook pair if you need to inject a
custom fetch in tests.

Do not use Mastra's `requireToolApproval` for this. That is Mastra's own
approval UI. These hooks talk to Mezmo.

## What the hooks send

Built-in tools use namespace `mastra`. Names of the form `mcp__<server>__<tool>`
split into that server and tool.

`request_id` is `mastra:<session_id>:<toolCallId>`. `session_id` is the memory
`threadId` when present, otherwise the workflow `runId`, then `agentId`. A
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

## Tests

Node 22+ (type stripping):

```bash
node --test test/*.test.ts
```

From the repo root: `make test-mastra`.
