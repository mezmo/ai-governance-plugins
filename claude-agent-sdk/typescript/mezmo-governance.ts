import { randomUUID } from "node:crypto";

import {
  AGENT,
  appendAudit,
  approvalPayload,
  argumentsObject,
  authorize,
  callIdentity,
  GovernanceError,
  resultFailed,
  resultPreview,
  splitToolName,
  type FetchLike,
} from "./client.ts";

// Claude Code cancels a callback at its matcher timeout and skips the tool. The
// client's 590 second deadline ends first, so the agent sees Mezmo's reason.
export const PRE_TOOL_USE_TIMEOUT_SECONDS = 600;
export const POST_TOOL_USE_TIMEOUT_SECONDS = 15;

// The fields this file reads from the SDK's PreToolUse, PostToolUse and
// PostToolUseFailure inputs. Declared here so the file runs without the SDK.
export type HookInput = {
  hook_event_name?: string;
  session_id?: string;
  tool_name?: string;
  tool_input?: unknown;
  tool_use_id?: string;
  tool_response?: unknown;
  error?: unknown;
};

export type PreToolUseDeny = {
  hookSpecificOutput: {
    hookEventName: "PreToolUse";
    permissionDecision: "deny";
    permissionDecisionReason: string;
  };
};

type Callback<T> = (
  input: HookInput,
  toolUseID: string | undefined,
  options: { signal: AbortSignal },
) => Promise<T>;

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new GovernanceError(`${field} must be a non-empty string`);
  }
  return value.trim();
}

function toolUseIdFrom(input: HookInput, toolUseID: string | undefined): string {
  for (const value of [toolUseID, input.tool_use_id]) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function deny(reason: string): PreToolUseDeny {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: reason,
    },
  };
}

export function createMezmoGovernanceHooks(options: { fetchImpl?: FetchLike } = {}) {
  const preToolUse: Callback<PreToolUseDeny | Record<string, never>> = async (input, toolUseID) => {
    // Any failure before a decision blocks the call: a callback that throws
    // hands the SDK an error instead of a deny.
    try {
      if (!input || typeof input !== "object" || Array.isArray(input)) {
        throw new GovernanceError("hook input must be a JSON object");
      }
      const payload = approvalPayload({
        sessionId: requiredString(input.session_id, "session_id"),
        // A call without an id gets a random one, so it never reuses a decision.
        toolCallId: toolUseIdFrom(input, toolUseID) || `generated-${randomUUID()}`,
        toolName: requiredString(input.tool_name, "tool_name"),
        arguments: argumentsObject(input.tool_input),
        eventName: "PreToolUse",
      });
      const decision = await authorize(payload, { fetchImpl: options.fetchImpl });
      if (decision.approved) {
        // Empty output: the SDK's own permission rules and mode still apply.
        return {};
      }
      const detail = decision.reason || "Policy denied the call.";
      return deny(`Mezmo Governance denied this exact tool call: ${detail}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return deny(
        `Mezmo Governance could not authorize this exact tool call, so the Claude Agent SDK blocked it: ${message}`,
      );
    }
  };

  const postToolUse: Callback<Record<string, never>> = async (input, toolUseID) => {
    try {
      const tool = splitToolName(requiredString(input?.tool_name, "tool_name"));
      const toolCallId = toolUseIdFrom(input, toolUseID);
      const sessionId = typeof input.session_id === "string" ? input.session_id.trim() : "";
      const identity = toolCallId && sessionId ? callIdentity(sessionId, toolCallId) : null;
      const failed = input.hook_event_name === "PostToolUseFailure" || resultFailed(input.tool_response);
      await appendAudit({
        ts: new Date().toISOString(),
        agent: AGENT,
        decision_id: identity?.decisionId ?? null,
        request_id: identity?.requestId ?? null,
        tool_namespace: tool.namespace,
        tool_name: tool.name,
        status: failed ? "failed" : "observed",
        result_preview: resultPreview(failed && input.error != null ? input.error : input.tool_response),
      });
    } catch (error) {
      console.error(
        `Mezmo Governance could not write the local audit log: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }
    return {};
  };

  // No matcher, so every tool call is checked, built-in and MCP alike.
  return {
    PreToolUse: [{ hooks: [preToolUse], timeout: PRE_TOOL_USE_TIMEOUT_SECONDS }],
    PostToolUse: [{ hooks: [postToolUse], timeout: POST_TOOL_USE_TIMEOUT_SECONDS }],
    PostToolUseFailure: [{ hooks: [postToolUse], timeout: POST_TOOL_USE_TIMEOUT_SECONDS }],
  };
}

export const mezmoGovernanceHooks = createMezmoGovernanceHooks();
