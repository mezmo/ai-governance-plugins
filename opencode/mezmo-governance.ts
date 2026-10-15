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

type AfterInput = BeforeInput;

type AfterOutput = {
  title?: string;
  output?: unknown;
  metadata?: unknown;
};

async function before(input: BeforeInput, output: BeforeOutput) {
  const payload = approvalPayload({
    sessionId: input.sessionID,
    toolCallId: input.callID,
    toolName: input.tool,
    arguments: argumentsObject(output.args ?? {}),
    eventName: "tool.execute.before",
  });
  try {
    const decision = await authorize(payload);
    if (!decision.approved) {
      const detail = decision.reason || "Policy denied the call.";
      throw new Error(`Mezmo Governance denied this exact tool call: ${detail}`);
    }
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

async function after(input: AfterInput, output: AfterOutput | undefined) {
  try {
    const identity = callIdentity(input.sessionID, input.callID);
    const tool = splitToolName(input.tool);
    await appendAudit({
      ts: new Date().toISOString(),
      agent: AGENT,
      decision_id: identity.decisionId,
      request_id: identity.requestId,
      tool_namespace: tool.namespace,
      tool_name: tool.name,
      status: afterFailed(output) ? "failed" : "observed",
      result_preview: resultPreview(output?.output ?? output ?? null),
    });
  } catch (error) {
    console.error(
      `Mezmo Governance could not write the local audit log: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

export const MezmoGovernance = async () => ({
  "tool.execute.before": before,
  "tool.execute.after": after,
});

export default MezmoGovernance;
