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
} from "./client.ts";

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

async function after(input: AfterInput, output: AfterOutput) {
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
      status: resultFailed(output) ? "failed" : "observed",
      result_preview: resultPreview(output.output ?? output),
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
