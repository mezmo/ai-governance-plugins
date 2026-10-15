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

async function before(input: BeforeInput, output: BeforeOutput, approved: Approved) {
  try {
    const args = argumentsObject(output.args ?? {});
    const payload = approvalPayload({
      sessionId: input.sessionID,
      toolCallId: input.callID,
      toolName: input.tool,
      arguments: args,
      eventName: "tool.execute.before",
    });
    const decision = await authorize(payload);
    if (!decision.approved) {
      const detail = decision.reason || "Policy denied the call.";
      throw new Error(`Mezmo Governance denied this exact tool call: ${detail}`);
    }
    if (approved.size >= MAX_PENDING) {
      approved.delete(approved.keys().next().value as string);
    }
    approved.set(`${input.sessionID}:${input.callID}`, JSON.stringify(args));
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

async function after(input: AfterInput, output: AfterOutput | undefined, approved: Approved) {
  try {
    const identity = callIdentity(input.sessionID, input.callID);
    const tool = splitToolName(input.tool);
    const key = `${input.sessionID}:${input.callID}`;
    const approvedArgs = approved.get(key);
    approved.delete(key);
    const changed = approvedArgs !== undefined && approvedArgs !== JSON.stringify(input.args ?? {});
    await appendAudit({
      ts: new Date().toISOString(),
      agent: AGENT,
      decision_id: identity.decisionId,
      request_id: identity.requestId,
      tool_namespace: tool.namespace,
      tool_name: tool.name,
      status: afterFailed(output) ? "failed" : "observed",
      result_preview: resultPreview(output?.output ?? output ?? null),
      ...(changed ? { arguments_changed: true } : {}),
    });
  } catch (error) {
    console.error(
      `Mezmo Governance could not write the local audit log: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

export const MezmoGovernance = async () => {
  const approved: Approved = new Map();
  return {
    "tool.execute.before": (input: BeforeInput, output: BeforeOutput) => before(input, output, approved),
    "tool.execute.after": (input: AfterInput, output: AfterOutput) => after(input, output, approved),
  };
};

export default MezmoGovernance;
