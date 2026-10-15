import { randomUUID } from "node:crypto";

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

export function sessionFromHook(
  context: unknown,
  metadata?: Record<string, unknown>,
): string {
  const ctx = asRecord(context);
  const agent = asRecord(ctx?.agent);
  const workflow = asRecord(ctx?.workflow);
  return (
    stringField(agent, "threadId") ||
    stringField(workflow, "runId") ||
    stringField(agent, "agentId") ||
    stringField(metadata, "agentId") ||
    "unknown"
  );
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
        const identity = callIdentity(sessionId, toolCallId);
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
