import {
  AGENT,
  appendAudit,
  approvalPayload,
  argumentsObject,
  authorize,
  callIdentity,
  resultPreview,
  splitToolName,
} from "./client.ts";

type SessionManager = {
  getSessionId?: () => string | undefined | null;
};

type Ui = {
  setStatus?: (id: string, text: string | undefined) => void;
};

type ToolCallEvent = {
  toolName: string;
  toolCallId: string;
  input: unknown;
};

type ToolResultEvent = {
  toolName: string;
  toolCallId: string;
  input?: unknown;
  content?: unknown;
  structuredContent?: unknown;
  isError?: boolean;
};

type ExtensionContext = {
  sessionManager: SessionManager;
  ui?: Ui;
};

type ExtensionAPI = {
  on(event: "tool_call", handler: (event: ToolCallEvent, ctx: ExtensionContext) => Promise<unknown> | unknown): void;
  on(event: "tool_result", handler: (event: ToolResultEvent, ctx: ExtensionContext) => Promise<unknown> | unknown): void;
};

function sessionId(ctx: ExtensionContext): string {
  const value = ctx.sessionManager.getSessionId?.();
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return "ephemeral";
}

// Pi runs tool_call handlers in load order, and a handler may mutate
// event.input in place. Remember what Mezmo approved so tool_result can flag a
// call whose arguments a later extension changed.
const MAX_PENDING = 1000;

export default function mezmoGovernance(pi: ExtensionAPI) {
  const approved = new Map<string, string>();

  pi.on("tool_call", async (event, ctx) => {
    ctx.ui?.setStatus?.("mezmo-governance", "Checking Mezmo Governance");
    try {
      const args = argumentsObject(event.input);
      const payload = approvalPayload({
        sessionId: sessionId(ctx),
        toolCallId: event.toolCallId,
        toolName: event.toolName,
        arguments: args,
        eventName: "tool_call",
      });
      const decision = await authorize(payload);
      if (!decision.approved) {
        const detail = decision.reason || "Policy denied the call.";
        return {
          block: true,
          reason: `Mezmo Governance denied this exact tool call: ${detail}`,
        };
      }
      if (approved.size >= MAX_PENDING) {
        approved.delete(approved.keys().next().value as string);
      }
      approved.set(event.toolCallId, JSON.stringify(args));
      return undefined;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return {
        block: true,
        reason: `Mezmo Governance could not authorize this exact tool call, so Pi blocked it: ${message}`,
      };
    } finally {
      ctx.ui?.setStatus?.("mezmo-governance", undefined);
    }
  });

  pi.on("tool_result", async (event, ctx) => {
    try {
      const identity = callIdentity(sessionId(ctx), event.toolCallId);
      const tool = splitToolName(event.toolName);
      const approvedArgs = approved.get(event.toolCallId);
      approved.delete(event.toolCallId);
      const changed = approvedArgs !== undefined && approvedArgs !== JSON.stringify(event.input ?? {});
      await appendAudit({
        ts: new Date().toISOString(),
        agent: AGENT,
        decision_id: identity.decisionId,
        request_id: identity.requestId,
        tool_namespace: tool.namespace,
        tool_name: tool.name,
        status: event.isError ? "failed" : "observed",
        result_preview: resultPreview(event.structuredContent ?? event.content ?? null),
        ...(changed ? { arguments_changed: true } : {}),
      });
    } catch (error) {
      console.error(
        `Mezmo Governance could not write the local audit log: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
  });
}
