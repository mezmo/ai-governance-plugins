import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { AGENT, callIdentity } from "../client.ts";
import {
  createMezmoGovernanceHooks,
  sessionFromHook,
  toolCallIdFromHook,
} from "../mezmo-governance.ts";

const URL = "https://api.example.test/governance/workflow/authorize";

function withEnv(values: Record<string, string>, fn: () => Promise<void>) {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  return fn().finally(() => {
    for (const [key, value] of previous) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  });
}

const ENV = {
  MEZMO_GOVERNANCE_URL: URL,
  MEZMO_GOVERNANCE_TOKEN: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

function jsonResponse(status: number, body: unknown) {
  return {
    status,
    async text() {
      return JSON.stringify(body);
    },
  };
}

test("session prefers threadId, then workflow runId, then agentId", () => {
  assert.equal(
    sessionFromHook({ agent: { threadId: "thread-1", agentId: "weather" } }),
    "thread-1",
  );
  assert.equal(
    sessionFromHook({ workflow: { runId: "run-9" } }),
    "run-9",
  );
  // agentId names the agent, not the run: a per-process nonce keeps an
  // approval from carrying over to the next run.
  const fallback = sessionFromHook({ agent: { agentId: "weather" } });
  assert.match(fallback, /^weather-[0-9a-f]{16}$/);
  assert.equal(sessionFromHook({ agent: { agentId: "weather" } }), fallback);
  const nonce = fallback.slice("weather-".length);
  assert.equal(sessionFromHook({}, { agentId: "from-meta" }), `from-meta-${nonce}`);
  assert.equal(sessionFromHook({}), `unknown-${nonce}`);
});

test("toolCallId prefers agent.toolCallId", () => {
  assert.equal(
    toolCallIdFromHook({ agent: { toolCallId: "call-1" } }),
    "call-1",
  );
  assert.equal(toolCallIdFromHook({}), "");
});

test(
  "beforeToolCall lets an approved call through",
  () =>
    withEnv(ENV, async () => {
      const hooks = createMezmoGovernanceHooks({
        fetchImpl: async () => jsonResponse(200, { approved: true }),
      });
      const result = await hooks.beforeToolCall({
        toolName: "weatherTool",
        input: { location: "Austin" },
        context: { agent: { threadId: "thread-1", toolCallId: "call-1" } },
      });
      assert.equal(result, undefined);
    }),
);

test(
  "beforeToolCall skips the tool when Mezmo denies",
  () =>
    withEnv(ENV, async () => {
      const hooks = createMezmoGovernanceHooks({
        fetchImpl: async () =>
          jsonResponse(200, { approved: false, reason: "not this location" }),
      });
      const result = await hooks.beforeToolCall({
        toolName: "weatherTool",
        input: { location: "Austin" },
        context: { agent: { threadId: "thread-1", toolCallId: "call-1" } },
      });
      assert.deepEqual(result, {
        proceed: false,
        output:
          "Mezmo Governance denied this exact tool call: not this location",
      });
    }),
);

test(
  "beforeToolCall skips the tool when Mezmo is unreachable",
  () =>
    withEnv(ENV, async () => {
      const hooks = createMezmoGovernanceHooks({
        fetchImpl: async () => {
          throw new Error("connect ECONNREFUSED");
        },
      });
      const result = await hooks.beforeToolCall({
        toolName: "weatherTool",
        input: { location: "Austin" },
        context: { agent: { threadId: "thread-1", toolCallId: "call-1" } },
      });
      assert.equal(result?.proceed, false);
      assert.match(String(result?.output), /could not authorize/);
    }),
);

test(
  "afterToolCall appends a local audit line",
  () =>
    withEnv(ENV, async () => {
      const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-"));
      const path = join(dir, "audit.jsonl");
      await withEnv({ MEZMO_GOVERNANCE_AUDIT_LOG: path }, async () => {
        const hooks = createMezmoGovernanceHooks({
          fetchImpl: async () => jsonResponse(200, { approved: true }),
        });
        await hooks.afterToolCall({
          toolName: "weatherTool",
          input: { location: "Austin" },
          context: { agent: { threadId: "thread-1", toolCallId: "call-1" } },
          output: { temperatureCelsius: 21 },
        });
        const line = JSON.parse((await readFile(path, "utf8")).trim());
        const identity = callIdentity("thread-1", "call-1", "weatherTool", { location: "Austin" });
        assert.equal(line.agent, AGENT);
        assert.equal(line.request_id, identity.requestId);
        assert.equal(line.tool_name, "weatherTool");
        assert.equal(line.status, "observed");
      });
    }),
);

test(
  "afterToolCall previews a thrown Error by name and message",
  () =>
    withEnv(ENV, async () => {
      const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-"));
      const path = join(dir, "audit.jsonl");
      await withEnv({ MEZMO_GOVERNANCE_AUDIT_LOG: path }, async () => {
        const hooks = createMezmoGovernanceHooks({
          fetchImpl: async () => jsonResponse(200, { approved: true }),
        });
        await hooks.afterToolCall({
          toolName: "weatherTool",
          input: { location: "Austin" },
          context: { agent: { threadId: "thread-1", toolCallId: "call-1" } },
          error: new TypeError("station offline"),
        });
        const line = JSON.parse((await readFile(path, "utf8")).trim());
        assert.equal(line.status, "failed");
        assert.deepEqual(JSON.parse(line.result_preview), {
          name: "TypeError",
          message: "station offline",
        });
      });
    }),
);

test(
  "identical calls without toolCallId each get a fresh decision",
  () =>
    withEnv(ENV, async () => {
      const decisionIds: string[] = [];
      const hooks = createMezmoGovernanceHooks({
        fetchImpl: async (_url, init) => {
          decisionIds.push(JSON.parse(String(init?.body)).decision_id);
          return jsonResponse(200, { approved: true });
        },
      });
      const context = { workflow: { runId: "run-1" } };
      for (let i = 0; i < 2; i += 1) {
        await hooks.beforeToolCall({ toolName: "weatherTool", input: { location: "Austin" }, context });
      }
      assert.equal(decisionIds.length, 2);
      assert.notEqual(decisionIds[0], decisionIds[1]);
    }),
);

test(
  "afterToolCall without toolCallId reuses the id its beforeToolCall sent",
  () =>
    withEnv(ENV, async () => {
      const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-"));
      const path = join(dir, "audit.jsonl");
      await withEnv({ MEZMO_GOVERNANCE_AUDIT_LOG: path }, async () => {
        let requestId = "";
        const hooks = createMezmoGovernanceHooks({
          fetchImpl: async (_url, init) => {
            requestId = JSON.parse(String(init?.body)).request_id;
            return jsonResponse(200, { approved: true });
          },
        });
        const hook = { toolName: "weatherTool", input: { location: "Austin" }, context: { workflow: { runId: "run-1" } } };
        await hooks.beforeToolCall(hook);
        await hooks.afterToolCall({ ...hook, output: { temperatureCelsius: 21 } });
        const line = JSON.parse((await readFile(path, "utf8")).trim());
        assert.equal(line.request_id, requestId);
      });
    }),
);
