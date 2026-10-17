import assert from "node:assert/strict";
import test from "node:test";

import { approvalPayload, authorize } from "../client.ts";

const ENV = {
  MEZMO_GOVERNANCE_URL: "https://api.example.test/governance/workflow/authorize",
  MEZMO_GOVERNANCE_TOKEN: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

async function withEnv(values: Record<string, string>, fn: () => Promise<void>) {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  try {
    await fn();
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
}

// Keeps decisions by decision_id as Mezmo does: a second POST gets 409.
function policyGovernance(deniedCommand: string) {
  const decisions = new Map<string, string>();
  const reply = (status: number, body: unknown) => ({ status, text: async () => JSON.stringify(body) });
  const fetchImpl = async (url: string, init?: { method?: string; body?: string }) => {
    if (init?.method === "GET") {
      return reply(200, { status: decisions.get(new URL(url).searchParams.get("decision_id") ?? "") });
    }
    const payload = JSON.parse(init?.body ?? "{}");
    if (decisions.has(payload.decision_id)) {
      return reply(409, {});
    }
    const status = payload.items[0].arguments.command === deniedCommand ? "denied" : "approved";
    decisions.set(payload.decision_id, status);
    return reply(200, { approved: status === "approved" });
  };
  return { decisions, fetchImpl };
}

async function ask(governance: ReturnType<typeof policyGovernance>, command: string) {
  const payload = approvalPayload({
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command },
  });
  return (await authorize(payload, { fetchImpl: governance.fetchImpl })).approved;
}

test("changed arguments under one call id get their own decision", () =>
  withEnv(ENV, async () => {
    const governance = policyGovernance("delete-important-data");
    assert.equal(await ask(governance, "ls"), true);
    assert.equal(await ask(governance, "delete-important-data"), false);
    assert.deepEqual([...governance.decisions.values()].sort(), ["approved", "denied"]);
  }));

test("a retry with the same arguments reuses the decision", () =>
  withEnv(ENV, async () => {
    const governance = policyGovernance("delete-important-data");
    assert.equal(await ask(governance, "ls"), true);
    assert.equal(await ask(governance, "ls"), true);
    assert.deepEqual([...governance.decisions.values()], ["approved"]);
  }));

test("the call digest ignores key order", () => {
  const first = approvalPayload({
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command: "ls", cwd: { path: "/tmp", depth: 1 } },
  });
  const second = approvalPayload({
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { cwd: { depth: 1, path: "/tmp" }, command: "ls" },
  });
  assert.equal(first.decision_id, second.decision_id);
});
