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
} from "./mezmo-governance/client.ts";

type BeforeInput = {
  tool: string;
  sessionID: string;
  callID: string;
};

type BeforeOutput = {
  args: unknown;
};

type AfterInput = BeforeInput & {
  args?: unknown;
};

type AfterOutput = {
  title?: string;
  output?: unknown;
  metadata?: unknown;
};

// OpenCode runs plugin hooks in load order and passes the same args object to
// each, then to the tool. Remember what Mezmo approved so the after hook can
// flag a call whose arguments a later plugin changed.
const MAX_PENDING = 1000;
type Approved = Map<string, string>;

// Ask Mezmo about one call; throw to stop it. Returns the approved arguments
// as JSON so the after hook can tell whether they changed.
async function gate(
  sessionId: string,
  toolCallId: string,
  toolName: string,
  rawArgs: unknown,
  eventName: string,
): Promise<string> {
  try {
    const args = argumentsObject(rawArgs ?? {});
    const payload = approvalPayload({ sessionId, toolCallId, toolName, arguments: args, eventName });
    const decision = await authorize(payload);
    if (!decision.approved) {
      const detail = decision.reason || "Policy denied the call.";
      throw new Error(`Mezmo Governance denied this exact tool call: ${detail}`);
    }
    return JSON.stringify(args);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("Mezmo Governance denied")) {
      throw error;
    }
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Mezmo Governance could not authorize this exact tool call, so OpenCode blocked it: ${message}`,
    );
  }
}

function remember<V>(map: Map<string, V>, key: string, value: V) {
  if (!map.has(key) && map.size >= MAX_PENDING) {
    map.delete(map.keys().next().value as string);
  }
  map.set(key, value);
}

async function before(input: BeforeInput, output: BeforeOutput, approved: Approved) {
  const args = await gate(input.sessionID, input.callID, input.tool, output.args, "tool.execute.before");
  remember(approved, `${input.sessionID}:${input.callID}`, args);
}

// Built-in tools return { title, output, metadata }; the shell tool reports its
// exit code as metadata.exit (null when killed). MCP tools return the raw
// result with isError. A missing result means the tool failed.
function afterFailed(output: AfterOutput | undefined): boolean {
  if (!output || typeof output !== "object") {
    return true;
  }
  const metadata = output.metadata;
  if (metadata && typeof metadata === "object" && "exit" in metadata) {
    const exit = (metadata as Record<string, unknown>).exit;
    if (exit === null || (typeof exit === "number" && exit !== 0)) {
      return true;
    }
  }
  return resultFailed(output);
}

type Observed = {
  sessionId: string;
  toolCallId: string;
  toolName: string;
  failed: boolean;
  preview: unknown;
  changed: boolean;
};

// The after hooks never throw: OpenCode 2.x turns a rejected execute.after
// into a defect, not a tool error.
async function audit(observe: () => Observed) {
  try {
    const seen = observe();
    const identity = callIdentity(seen.sessionId, seen.toolCallId);
    const tool = splitToolName(seen.toolName);
    await appendAudit({
      ts: new Date().toISOString(),
      agent: AGENT,
      decision_id: identity.decisionId,
      request_id: identity.requestId,
      tool_namespace: tool.namespace,
      tool_name: tool.name,
      status: seen.failed ? "failed" : "observed",
      result_preview: resultPreview(seen.preview),
      ...(seen.changed ? { arguments_changed: true } : {}),
    });
  } catch (error) {
    console.error(
      `Mezmo Governance could not write the local audit log: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

function after(input: AfterInput, output: AfterOutput | undefined, approved: Approved) {
  return audit(() => {
    const key = `${input.sessionID}:${input.callID}`;
    const approvedArgs = approved.get(key);
    approved.delete(key);
    return {
      sessionId: input.sessionID,
      toolCallId: input.callID,
      toolName: input.tool,
      failed: afterFailed(output),
      preview: output?.output ?? output ?? null,
      changed: approvedArgs !== undefined && approvedArgs !== JSON.stringify(input.args ?? {}),
    };
  });
}

// OpenCode 2.x: tool.hook("execute.before") may throw to fail that call
// alone. Code Mode runs every inner tool call of one `execute` call with the
// outer call id, so each call sends `<id>#<random>`: no two calls can share a
// decision, however many other calls run in between.
type V2Before = { tool: string; sessionID: string; id: string; input: unknown };
type V2After = V2Before &
  (
    | { status: "completed"; result: AfterOutput & { content?: unknown } }
    | { status: "error"; error: unknown }
  );
type V2Context = {
  tool: {
    hook(name: "execute.before", callback: (event: V2Before) => Promise<void>): Promise<unknown>;
    hook(name: "execute.after", callback: (event: V2After) => Promise<void>): Promise<unknown>;
  };
};
type Pending = { tool: string; args: string; callId: string };
// Approved calls waiting for their after hook, per `<sessionID>:<id>`. Losing
// an evicted entry only loses the audit line's pairing, never a decision.
type Calls = Map<string, Pending[]>;

async function beforeV2(event: V2Before, calls: Calls) {
  const callId = `${event.id}#${randomUUID()}`;
  const args = await gate(event.sessionID, callId, event.tool, event.input, "execute.before");
  const key = `${event.sessionID}:${event.id}`;
  const pending = calls.get(key) ?? [];
  pending.push({ tool: event.tool, args, callId });
  remember(calls, key, pending);
}

function afterV2(event: V2After, calls: Calls) {
  return audit(() => {
    const key = `${event.sessionID}:${event.id}`;
    const pending = calls.get(key) ?? [];
    const args = JSON.stringify(event.input ?? {});
    const exact = pending.findIndex((item) => item.tool === event.tool && item.args === args);
    const index = exact >= 0 ? exact : pending.findIndex((item) => item.tool === event.tool);
    const [match] = pending.splice(index >= 0 ? index : 0, 1);
    if (!pending.length) {
      calls.delete(key);
    }
    return {
      sessionId: event.sessionID,
      toolCallId: match?.callId ?? event.id,
      toolName: event.tool,
      failed: event.status === "error" || afterFailed(event.result),
      preview: event.status === "error" ? event.error : (event.result.output ?? event.result.content ?? null),
      // A later hook may change the tool or its input after Mezmo approved it.
      changed: match !== undefined && exact < 0,
    };
  });
}

export const MezmoGovernance = async () => {
  const approved: Approved = new Map();
  return {
    "tool.execute.before": (input: BeforeInput, output: BeforeOutput) => before(input, output, approved),
    "tool.execute.after": (input: AfterInput, output: AfterOutput) => after(input, output, approved),
  };
};

// One default export for both majors: OpenCode 1.x calls `server`, 2.x calls
// `setup`, and each ignores the other key.
export default {
  id: "mezmo-governance",
  server: MezmoGovernance,
  async setup(context: V2Context) {
    const calls: Calls = new Map();
    await context.tool.hook("execute.before", (event) => beforeV2(event, calls));
    await context.tool.hook("execute.after", (event) => afterV2(event, calls));
  },
};
