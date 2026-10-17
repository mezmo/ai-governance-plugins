import assert from "node:assert/strict";
import test from "node:test";

import { callIdentity } from "../mezmo-governance/client.ts";
import plugin from "../mezmo-governance.ts";
import { auditLines, decision, type FakeFetch, withFakeMezmo } from "./runtime.ts";

type Hook = (event: any) => Promise<void>;

// A fake OpenCode 2.x setup context: tool.hook records each callback, as
// packages/plugin/src/promise/adapter.ts registers it.
async function withV2(fetchImpl: FakeFetch, fn: (hooks: Record<string, Hook>, audit: string) => Promise<void>) {
  await withFakeMezmo(fetchImpl, async (audit) => {
    const hooks: Record<string, Hook> = {};
    await plugin.setup({
      tool: {
        hook: async (name: string, callback: Hook) => {
          hooks[name] = callback;
          return { dispose: async () => {} };
        },
      },
    } as any);
    await fn(hooks, audit);
  });
}

function recording(sent: any[], body: unknown = { approved: true }): FakeFetch {
  return async (_url, init) => {
    sent.push(JSON.parse(String(init?.body)));
    return { status: 200, text: async () => JSON.stringify(body) };
  };
}

// Fields from packages/core/src/tool.ts beforeExecute and executeTool.
const CALL_1 = /^opencode:ses_1:call_1#[0-9a-f-]{36}#[0-9a-f]{16}$/;
const BASE = { tool: "bash", sessionID: "ses_1", agent: "build", messageID: "msg_1", id: "call_1" };

test("setup registers execute.before and execute.after", async () => {
  await withV2(decision({ approved: true }), async (hooks) => {
    assert.deepEqual(Object.keys(hooks).sort(), ["execute.after", "execute.before"]);
  });
});

test("execute.before resolves when Mezmo approves", async () => {
  const sent: any[] = [];
  await withV2(recording(sent), async (hooks) => {
    await hooks["execute.before"]({ ...BASE, input: { command: "ls" } });
  });
  assert.match(sent[0].request_id, CALL_1);
  assert.equal(sent[0].origin.reason, "execute.before");
  assert.equal(sent[0].items[0].tool_name, "bash");
  assert.deepEqual(sent[0].items[0].arguments, { command: "ls" });
});

test("execute.before rejects with the reason when Mezmo denies", async () => {
  await withV2(decision({ approved: false, reason: "no shell" }), async (hooks) => {
    await assert.rejects(
      hooks["execute.before"]({ ...BASE, input: { command: "ls" } }),
      /Mezmo Governance denied this exact tool call: no shell/,
    );
  });
});

test("execute.before rejects when Mezmo is unreachable", async () => {
  await withV2(
    async () => {
      throw new Error("connect ECONNREFUSED");
    },
    async (hooks) => {
      await assert.rejects(
        hooks["execute.before"]({ ...BASE, input: { command: "ls" } }),
        /could not authorize this exact tool call, so OpenCode blocked it: Mezmo Governance is unavailable$/,
      );
    },
  );
});

// packages/core/src/codemode/tool.ts runs each inner call with the outer
// `execute` call's context, so they all share its id.
test("Code Mode inner calls under one id each get their own decision", async () => {
  const sent: any[] = [];
  await withV2(recording(sent), async (hooks, audit) => {
    await hooks["execute.before"]({ ...BASE, tool: "execute", input: { code: "..." } });
    await hooks["execute.before"]({ ...BASE, tool: "github_issues", input: { repo: "a" } });
    await hooks["execute.before"]({ ...BASE, tool: "github_issues", input: { repo: "b" } });
    const done = { status: "completed", result: { content: "ok" } };
    await hooks["execute.after"]({ ...BASE, tool: "github_issues", input: { repo: "b" }, ...done });
    await hooks["execute.after"]({ ...BASE, tool: "github_issues", input: { repo: "a" }, ...done });
    await hooks["execute.after"]({ ...BASE, tool: "execute", input: { code: "..." }, ...done });

    const ids = sent.map((payload) => payload.request_id);
    assert.ok(ids.every((id) => CALL_1.test(id)), ids.join());
    assert.equal(new Set(sent.map((payload) => payload.decision_id)).size, 3);
    const lines = await auditLines(audit);
    assert.deepEqual(
      lines.map((line) => line.request_id),
      [ids[2], ids[1], ids[0]],
    );
    const callId = ids[2].slice("opencode:ses_1:".length).replace(/#[0-9a-f]{16}$/, "");
    assert.equal(lines[0].decision_id, callIdentity("ses_1", callId, "github_issues", { repo: "b" }).decisionId);
    assert.ok(lines.every((line) => line.status === "observed" && !line.arguments_changed));
  });
});

test("a Code Mode call never reuses a decision, even after 1,000 other calls", async () => {
  const sent: any[] = [];
  await withV2(recording(sent), async (hooks) => {
    await hooks["execute.before"]({ ...BASE, tool: "execute", input: { code: "..." } });
    for (let index = 0; index < 1000; index += 1) {
      await hooks["execute.before"]({ ...BASE, id: `other_${index}`, input: { command: "ls" } });
    }
    await hooks["execute.before"]({ ...BASE, tool: "github_issues", input: { repo: "a" } });
  });
  const mine = sent.filter((payload) => payload.request_id.startsWith("opencode:ses_1:call_1"));
  assert.equal(mine.length, 2);
  assert.notEqual(mine[0].decision_id, mine[1].decision_id);
});

test("execute.after records a Tool.Error as failed with its message", async () => {
  await withV2(decision({ approved: true }), async (hooks, audit) => {
    await hooks["execute.before"]({ ...BASE, input: { command: "ls" } });
    await hooks["execute.after"]({
      ...BASE,
      input: { command: "ls" },
      status: "error",
      error: new Error("permission denied"),
    });
    const [line] = await auditLines(audit);
    assert.equal(line.status, "failed");
    assert.equal(JSON.parse(line.result_preview).message, "permission denied");
  });
});

test("execute.after records a non-zero shell exit as failed", async () => {
  await withV2(decision({ approved: true }), async (hooks, audit) => {
    await hooks["execute.after"]({
      ...BASE,
      input: { command: "false" },
      status: "completed",
      result: { content: "", metadata: { exit: 1 } },
    });
    assert.equal((await auditLines(audit))[0].status, "failed");
  });
});

test("execute.after flags a tool or input a later hook changed after approval", async () => {
  await withV2(decision({ approved: true }), async (hooks, audit) => {
    const event = { ...BASE, input: { command: "ls" } };
    await hooks["execute.before"](event);
    event.input = { command: "rm -rf /tmp/x" };
    await hooks["execute.after"]({ ...event, status: "completed", result: { content: "" } });
    const [line] = await auditLines(audit);
    assert.equal(line.arguments_changed, true);
    assert.match(line.request_id, CALL_1);
    // The line still joins the decision Mezmo made on the approved arguments.
    const callId = line.request_id.slice("opencode:ses_1:".length).replace(/#[0-9a-f]{16}$/, "");
    assert.equal(line.request_id, callIdentity("ses_1", callId, "bash", { command: "ls" }).requestId);
  });
});

// A rejected execute.after is a defect in 2.x (Effect.promise), not a tool error.
test("execute.after never rejects", async () => {
  await withV2(decision({ approved: true }), async (hooks) => {
    await hooks["execute.after"]({ ...BASE, input: { n: 1n }, status: "completed", result: {} });
  });
});
