# Mastra + Mezmo Governance

Mastra agent hooks that ask Mezmo Governance before each tool runs. Denied or
failed checks skip the tool (`proceed: false`). After an approved tool finishes,
`afterToolCall` appends a local JSONL record.

Requires `@mastra/core` 1.49.0 or later (agent `hooks`). Tested against
`@mastra/core` 1.77.0: a type check against its `ToolHooks` and
`Agent.generate` options, plus tests that call the hooks directly. Official
docs: https://mastra.ai/docs/agents/using-tools

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

## Verify it works

From the folder that holds `mezmo-governance.ts`, ask the hook about one call
(Node 22+):

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
