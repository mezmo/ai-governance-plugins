import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { callIdentity } from "../mezmo-governance/client.ts";
import { MezmoGovernance } from "../mezmo-governance.ts";

const ENV = {
  MEZMO_GOVERNANCE_URL: "https://api.example.test/governance/workflow/authorize",
  MEZMO_GOVERNANCE_ACCESS_KEY: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

type FakeFetch = (url: string, init?: { body?: string }) => Promise<unknown>;

async function withRuntime(
  fetchImpl: FakeFetch,
  fn: (hooks: Awaited<ReturnType<typeof MezmoGovernance>>, audit: string) => Promise<void>,
) {
  const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-oc-"));
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
    await fn(await MezmoGovernance(), audit);
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

const INPUT = { tool: "bash", sessionID: "ses_1", callID: "call_1" };

async function auditLine(path: string) {
  return JSON.parse((await readFile(path, "utf8")).trim());
}

test("tool.execute.before returns when Mezmo approves", async () => {
  let sent: Record<string, any> = {};
  await withRuntime(
    async (_url, init) => {
      sent = JSON.parse(String(init?.body));
      return { status: 200, text: async () => '{"approved":true}' };
    },
    async (hooks) => {
      await hooks["tool.execute.before"](INPUT, { args: { command: "ls" } });
    },
  );
  assert.equal(sent.request_id, "opencode:ses_1:call_1");
  assert.deepEqual(sent.items[0].arguments, { command: "ls" });
});

test("tool.execute.before throws the reason when Mezmo denies", async () => {
  await withRuntime(decision({ approved: false, reason: "no shell" }), async (hooks) => {
    await assert.rejects(
      hooks["tool.execute.before"](INPUT, { args: { command: "ls" } }),
      /Mezmo Governance denied this exact tool call: no shell/,
    );
  });
});

test("tool.execute.before throws when Mezmo is unreachable", async () => {
  await withRuntime(
    async () => {
      throw new Error("connect ECONNREFUSED");
    },
    async (hooks) => {
      await assert.rejects(
        hooks["tool.execute.before"](INPUT, { args: { command: "ls" } }),
        /could not authorize this exact tool call, so OpenCode blocked it/,
      );
    },
  );
});

test("tool.execute.after records shell output and exit 0 as observed", async () => {
  await withRuntime(decision({ approved: true }), async (hooks, audit) => {
    await hooks["tool.execute.after"](
      { ...INPUT, args: { command: "ls" } },
      { title: "ls", output: "a\nb", metadata: { exit: 0, output: "a\nb" } },
    );
    const line = await auditLine(audit);
    assert.equal(line.request_id, callIdentity("ses_1", "call_1").requestId);
    assert.equal(line.status, "observed");
    assert.equal(line.result_preview, JSON.stringify("a\nb"));
  });
});

test("tool.execute.after records a non-zero shell exit as failed", async () => {
  await withRuntime(decision({ approved: true }), async (hooks, audit) => {
    await hooks["tool.execute.after"](
      { ...INPUT, args: { command: "false" } },
      { title: "false", output: "(no output)", metadata: { exit: 1, output: "" } },
    );
    assert.equal((await auditLine(audit)).status, "failed");
  });
});

test("tool.execute.after records a killed shell (exit null) as failed", async () => {
  await withRuntime(decision({ approved: true }), async (hooks, audit) => {
    await hooks["tool.execute.after"](
      { ...INPUT, args: { command: "sleep 999" } },
      { title: "sleep 999", output: "", metadata: { exit: null, output: "" } },
    );
    assert.equal((await auditLine(audit)).status, "failed");
  });
});

test("tool.execute.after records an MCP isError result as failed", async () => {
  await withRuntime(decision({ approved: true }), async (hooks, audit) => {
    await hooks["tool.execute.after"](
      { ...INPUT, tool: "kubernetes_pods_delete", args: {} },
      { content: [{ type: "text", text: "forbidden" }], isError: true } as any,
    );
    assert.equal((await auditLine(audit)).status, "failed");
  });
});

test("tool.execute.after records a missing result as failed", async () => {
  await withRuntime(decision({ approved: true }), async (hooks, audit) => {
    await hooks["tool.execute.after"]({ ...INPUT, tool: "task", args: {} }, undefined as any);
    assert.equal((await auditLine(audit)).status, "failed");
  });
});
