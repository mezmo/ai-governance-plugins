import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { callIdentity } from "../client.ts";
import mezmoGovernance from "../mezmo-governance.ts";

const ENV = {
  MEZMO_GOVERNANCE_URL: "https://api.example.test/governance/workflow/authorize",
  MEZMO_GOVERNANCE_ACCESS_KEY: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

type Handler = (event: any, ctx: any) => Promise<unknown> | unknown;
type FakeFetch = (url: string, init?: { body?: string }) => Promise<unknown>;

// A fake Pi runtime: records handlers by event name, like ExtensionAPI.on.
function fakePi() {
  const handlers = new Map<string, Handler>();
  const statuses: Array<[string, string | undefined]> = [];
  const ctx = {
    sessionManager: { getSessionId: () => "sess-1" },
    ui: { setStatus: (id: string, text: string | undefined) => statuses.push([id, text]) },
  };
  mezmoGovernance({
    on(event: string, handler: Handler) {
      handlers.set(event, handler);
    },
  } as any);
  return {
    statuses,
    toolCall: (event: Record<string, unknown>) => handlers.get("tool_call")!({ type: "tool_call", ...event }, ctx),
    toolResult: (event: Record<string, unknown>) =>
      handlers.get("tool_result")!({ type: "tool_result", ...event }, ctx),
  };
}

async function withRuntime(fetchImpl: FakeFetch, fn: (audit: string) => Promise<void>) {
  const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-pi-"));
  const audit = join(dir, "audit.jsonl");
  const values: Record<string, string> = { ...ENV, MEZMO_GOVERNANCE_AUDIT_LOG: audit };
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  const realFetch = globalThis.fetch;
  globalThis.fetch = fetchImpl as typeof fetch;
  try {
    await fn(audit);
  } finally {
    globalThis.fetch = realFetch;
    for (const [key, value] of previous) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
}

function decision(body: unknown): FakeFetch {
  return async () => ({ status: 200, text: async () => JSON.stringify(body) });
}

async function auditLine(path: string) {
  return JSON.parse((await readFile(path, "utf8")).trim());
}

test("tool_call returns nothing when Mezmo approves", async () => {
  let sent: Record<string, any> = {};
  await withRuntime(
    async (_url, init) => {
      sent = JSON.parse(String(init?.body));
      return { status: 200, text: async () => '{"approved":true}' };
    },
    async () => {
      const pi = fakePi();
      const result = await pi.toolCall({ toolName: "bash", toolCallId: "call-1", input: { command: "ls" } });
      assert.equal(result, undefined);
      assert.deepEqual(pi.statuses.at(-1), ["mezmo-governance", undefined]);
    },
  );
  assert.equal(sent.request_id, "pi:sess-1:call-1");
  assert.deepEqual(sent.items[0].arguments, { command: "ls" });
});

test("tool_call blocks with the reason when Mezmo denies", async () => {
  await withRuntime(decision({ approved: false, reason: "no shell" }), async () => {
    const result = await fakePi().toolCall({ toolName: "bash", toolCallId: "call-1", input: { command: "ls" } });
    assert.deepEqual(result, {
      block: true,
      reason: "Mezmo Governance denied this exact tool call: no shell",
    });
  });
});

test("tool_call blocks when Mezmo is unreachable", async () => {
  await withRuntime(
    async () => {
      throw new Error("connect ECONNREFUSED");
    },
    async () => {
      const result = (await fakePi().toolCall({
        toolName: "bash",
        toolCallId: "call-1",
        input: { command: "ls" },
      })) as { block: boolean; reason: string };
      assert.equal(result.block, true);
      assert.match(result.reason, /could not authorize this exact tool call, so Pi blocked it/);
    },
  );
});

test("tool_result previews content and records success", async () => {
  await withRuntime(decision({ approved: true }), async (audit) => {
    await fakePi().toolResult({
      toolName: "bash",
      toolCallId: "call-1",
      input: { command: "ls" },
      content: [{ type: "text", text: "a\nb" }],
      details: undefined,
      isError: false,
    });
    const line = await auditLine(audit);
    assert.equal(line.request_id, callIdentity("sess-1", "call-1").requestId);
    assert.equal(line.status, "observed");
    assert.match(line.result_preview, /a\\nb/);
  });
});

test("tool_result prefers structuredContent and records isError as failed", async () => {
  await withRuntime(decision({ approved: true }), async (audit) => {
    await fakePi().toolResult({
      toolName: "mcp__kubernetes__pods_delete",
      toolCallId: "call-2",
      input: {},
      content: [{ type: "text", text: "forbidden" }],
      structuredContent: { code: 403 },
      isError: true,
    });
    const line = await auditLine(audit);
    assert.equal(line.tool_namespace, "kubernetes");
    assert.equal(line.status, "failed");
    assert.equal(line.result_preview, '{"code":403}');
  });
});
