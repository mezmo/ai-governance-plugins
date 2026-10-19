import { randomBytes, randomUUID } from "node:crypto";

import {
  AGENT,
  appendAudit,
  approvalPayload,
  argumentsObject,
  authorize,
  callIdentity,
  resultFailed,
  resultPreview,
  splitToolName,
  type FetchLike,
} from "./client.ts";

export type HookContext = {
  toolName: string;
  input?: unknown;
  context?: unknown;
  metadata?: Record<string, unknown>;
  output?: unknown;
  error?: unknown;
};

export type BeforeHookResult = {
  proceed: false;
  output: string;
};

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return undefined;
}

function stringField(record: Record<string, unknown> | undefined, key: string): string {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

// agentId names the agent, not the run: a per-process nonce keeps an
// approval from carrying over to the next run.
const PROCESS_NONCE = randomBytes(8).toString("hex");

export function sessionFromHook(
  context: unknown,
  metadata?: Record<string, unknown>,
): string {
  const ctx = asRecord(context);
  const agent = asRecord(ctx?.agent);
  const workflow = asRecord(ctx?.workflow);
  const run = stringField(agent, "threadId") || stringField(workflow, "runId");
  if (run) {
    return run;
  }
  const name = stringField(agent, "agentId") || stringField(metadata, "agentId") || "unknown";
  return `${name}-${PROCESS_NONCE}`;
}

export function toolCallIdFromHook(context: unknown): string {
  return stringField(asRecord(asRecord(context)?.agent), "toolCallId");
}

// Without a runtime toolCallId, every call gets a fresh random id, so two
// identical calls never share one decision. afterToolCall receives the same
// context object as beforeToolCall, which lets it find that id for the audit.
function fallbackIds() {
  const pending = new WeakMap<object, string>();
  return {
    before(hook: HookContext): string {
      const fromAgent = toolCallIdFromHook(hook.context);
      if (fromAgent) {
        return fromAgent;
      }
      const id = randomUUID();
      const key = asRecord(hook.context);
      if (key) {
        pending.set(key, id);
      }
      return id;
    },
    after(hook: HookContext): string {
      const fromAgent = toolCallIdFromHook(hook.context);
      if (fromAgent) {
        return fromAgent;
      }
      const key = asRecord(hook.context);
      const id = key && pending.get(key);
      if (key) {
        pending.delete(key);
      }
      return id || randomUUID();
    },
  };
}

function deny(message: string): BeforeHookResult {
  return { proceed: false, output: message };
}

export function createMezmoGovernanceHooks(options: { fetchImpl?: FetchLike } = {}) {
  const ids = fallbackIds();
  return {
    async beforeToolCall(hook: HookContext): Promise<BeforeHookResult | undefined> {
      try {
        const payload = approvalPayload({
          sessionId: sessionFromHook(hook.context, hook.metadata),
          toolCallId: ids.before(hook),
          toolName: hook.toolName,
          arguments: argumentsObject(hook.input ?? {}),
          eventName: "beforeToolCall",
        });
        const decision = await authorize(payload, { fetchImpl: options.fetchImpl });
        if (decision.approved) {
          return undefined;
        }
        const detail = decision.reason || "Policy denied the call.";
        return deny(`Mezmo Governance denied this exact tool call: ${detail}`);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return deny(
          `Mezmo Governance could not authorize this exact tool call, so Mastra skipped it: ${message}`,
        );
      }
    },

    async afterToolCall(hook: HookContext): Promise<void> {
      try {
        const sessionId = sessionFromHook(hook.context, hook.metadata);
        const toolCallId = ids.after(hook);
        const identity = callIdentity(sessionId, toolCallId, hook.toolName, hook.input ?? {});
        const tool = splitToolName(hook.toolName);
        await appendAudit({
          ts: new Date().toISOString(),
          agent: AGENT,
          decision_id: identity.decisionId,
          request_id: identity.requestId,
          tool_namespace: tool.namespace,
          tool_name: tool.name,
          status: hook.error || resultFailed(hook.output) ? "failed" : "observed",
          result_preview: resultPreview(hook.error ?? hook.output),
        });
      } catch (error) {
        console.error(
          `Mezmo Governance could not write the local audit log: ${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
    },
  };
}

export const mezmoGovernanceHooks = createMezmoGovernanceHooks();

type GovernanceHooks = ReturnType<typeof createMezmoGovernanceHooks>;

// Mastra's own wrapper (Agent.wrapToolWithHooks in @mastra/core 1.77.0), for
// tools Mastra builds without it. The copy keeps the tool's prototype, so it
// still passes Mastra's `instanceof Tool` check.
function governTool(toolName: string, tool: unknown, hooks: GovernanceHooks): unknown {
  const execute = asRecord(tool)?.execute;
  if (typeof execute !== "function") {
    return tool;
  }
  const copy = Object.create(
    Object.getPrototypeOf(tool),
    Object.getOwnPropertyDescriptors(tool),
  );
  Object.defineProperty(copy, "execute", {
    configurable: true,
    enumerable: true,
    writable: true,
    async value(input: unknown, context: unknown) {
      const hook: HookContext = { toolName, input, context };
      const before = await hooks.beforeToolCall(hook);
      if (before?.proceed === false) {
        return before.output;
      }
      let output: unknown;
      try {
        output = await execute.call(tool, input, context);
      } catch (error) {
        await hooks.afterToolCall({ ...hook, error });
        throw error;
      }
      await hooks.afterToolCall({ ...hook, output });
      return output;
    },
  });
  return copy;
}

// Mastra rebuilds the tools that `prepareStep` or an input processor's
// `processInputStep` returns without the agent hooks. Pass new tools through
// this so each call still asks Mezmo first.
export function governTools<T extends Record<string, unknown>>(
  tools: T,
  hooks: GovernanceHooks = mezmoGovernanceHooks,
): T {
  return Object.fromEntries(
    Object.entries(tools).map(([name, tool]) => [name, governTool(name, tool, hooks)]),
  ) as T;
}
