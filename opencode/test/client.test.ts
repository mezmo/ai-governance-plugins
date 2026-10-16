import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  AGENT,
  appendAudit,
  approvalPayload,
  authorizationHeader,
  authorize,
  callIdentity,
  GovernanceError,
  splitToolName,
} from "../client.ts";

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
  MEZMO_GOVERNANCE_ACCESS_KEY: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

test("splits MCP tool names", () => {
  assert.deepEqual(splitToolName("mcp__kubernetes_demo__pods_delete"), {
    namespace: "kubernetes_demo",
    name: "pods_delete",
  });
  assert.deepEqual(splitToolName("bash"), { namespace: AGENT, name: "bash" });
});

test("builds a stable identity", () => {
  const first = callIdentity("sess-1", "call-1");
  const second = callIdentity("sess-1", "call-1");
  assert.equal(first.requestId, "opencode:sess-1:call-1");
  assert.equal(first.decisionId, second.decisionId);
});

test("payload origin is opencode_plugin", () => {
  const payload = approvalPayload({
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command: "ls" },
  });
  assert.deepEqual(payload.origin, { kind: "opencode_plugin", reason: "tool.execute.before" });
});

test(
  "prefixes Token on the access key",
  () =>
    withEnv(ENV, async () => {
      assert.equal(authorizationHeader(), "Token sts_test");
    }),
);

test(
  "allows an approved call",
  () =>
    withEnv(ENV, async () => {
      const calls: string[] = [];
      const fetchImpl = async (input: string, init?: { method?: string }) => {
        calls.push(`${init?.method ?? "GET"} ${input}`);
        return {
          status: 200,
          async text() {
            return JSON.stringify({ approved: true });
          },
        };
      };
      const payload = approvalPayload({
        sessionId: "sess-1",
        toolCallId: "call-1",
        toolName: "bash",
        arguments: { command: "ls" },
      });
      const decision = await authorize(payload, { fetchImpl });
      assert.equal(decision.approved, true);
      assert.equal(calls[0], `POST ${URL}`);
    }),
);

test(
  "polls after timeout then allows",
  () =>
    withEnv(ENV, async () => {
      let step = 0;
      let now = 0;
      const fetchImpl = async () => {
        step += 1;
        if (step === 1) {
          throw Object.assign(new Error("aborted"), { name: "AbortError" });
        }
        if (step === 2) {
          return {
            status: 200,
            async text() {
              return JSON.stringify({ status: "pending", reason: null });
            },
          };
        }
        return {
          status: 200,
          async text() {
            return JSON.stringify({ status: "approved", reason: "ok" });
          },
        };
      };
      const payload = approvalPayload({
        sessionId: "sess-1",
        toolCallId: "call-1",
        toolName: "bash",
        arguments: { command: "ls" },
      });
      const decision = await authorize(payload, {
        fetchImpl,
        now: () => now,
        sleep: async (ms) => {
          now += ms;
        },
      });
      assert.equal(decision.approved, true);
    }),
);

test(
  "polls after 409 then denies",
  () =>
    withEnv(ENV, async () => {
      let step = 0;
      const fetchImpl = async () => {
        step += 1;
        if (step === 1) {
          return {
            status: 409,
            async text() {
              return JSON.stringify({ code: "ECONFLICT" });
            },
          };
        }
        return {
          status: 200,
          async text() {
            return JSON.stringify({ status: "denied", reason: "already decided" });
          },
        };
      };
      const payload = approvalPayload({
        sessionId: "sess-1",
        toolCallId: "call-1",
        toolName: "bash",
        arguments: { command: "ls" },
      });
      const decision = await authorize(payload, { fetchImpl });
      assert.equal(decision.approved, false);
      assert.equal(decision.reason, "already decided");
    }),
);

test(
  "waits for a person after 207 then allows",
  () =>
    withEnv(ENV, async () => {
      const responses = [
        { status: 207, body: { approved: false } },
        { status: 200, body: { status: "pending", reason: null } },
        { status: 200, body: { status: "approved", reason: "looked it over" } },
      ];
      const calls: Array<[string | undefined, string]> = [];
      let now = 0;
      const fetchImpl = async (url: string, init?: { method?: string }) => {
        calls.push([init?.method, url]);
        const next = responses.shift()!;
        return {
          status: next.status,
          async text() {
            return JSON.stringify(next.body);
          },
        };
      };
      const payload = approvalPayload({
        sessionId: "sess-1",
        toolCallId: "call-1",
        toolName: "bash",
        arguments: { command: "ls" },
      });
      const decision = await authorize(payload, {
        fetchImpl,
        now: () => now,
        sleep: async (ms) => {
          now += ms;
        },
      });
      assert.equal(decision.approved, true);
      assert.equal(decision.reason, "looked it over");
      const pollUrl = `${URL}?decision_id=${payload.decision_id}`;
      assert.deepEqual(calls, [
        ["POST", URL],
        ["GET", pollUrl],
        ["GET", pollUrl],
      ]);
      assert.equal(now, 1000);
    }),
);

test(
  "polls through 207 until a person approves",
  () =>
    withEnv(ENV, async () => {
      // What Mezmo answers while a person decides: 207 with "approved": false,
      // on the POST and on each poll. Then 200 with the decision.
      const responses = [
        { status: 207, body: { approved: false, reason: "No rule in this policy covers this request." } },
        { status: 207, body: { approved: false, reason: null } },
        { status: 200, body: { approved: true, reason: "looked it over" } },
      ];
      const calls: Array<[string | undefined, string]> = [];
      let now = 0;
      const fetchImpl = async (url: string, init?: { method?: string }) => {
        calls.push([init?.method, url]);
        const next = responses.shift()!;
        return {
          status: next.status,
          async text() {
            return JSON.stringify(next.body);
          },
        };
      };
      const payload = approvalPayload({ sessionId: "sess-1", toolCallId: "call-1", toolName: "bash", arguments: { command: "ls" } });
      const decision = await authorize(payload, {
        fetchImpl,
        now: () => now,
        sleep: async (ms) => {
          now += ms;
        },
      });
      assert.equal(decision.approved, true);
      assert.equal(decision.reason, "looked it over");
      const pollUrl = `${URL}?decision_id=${payload.decision_id}`;
      assert.deepEqual(calls, [
        ["POST", URL],
        ["GET", pollUrl],
        ["GET", pollUrl],
      ]);
      assert.equal(now, 1000);
    }),
);

test(
  "a person denies through the poll",
  () =>
    withEnv(ENV, async () => {
      const responses = [
        { status: 207, body: { approved: false } },
        { status: 200, body: { approved: false, reason: "not today" } },
      ];
      const fetchImpl = async () => {
        const next = responses.shift()!;
        return {
          status: next.status,
          async text() {
            return JSON.stringify(next.body);
          },
        };
      };
      let now = 0;
      const decision = await authorize(approvalPayload({ sessionId: "sess-1", toolCallId: "call-1", toolName: "bash", arguments: { command: "ls" } }), {
        fetchImpl,
        now: () => now,
        sleep: async (ms) => {
          now += ms;
        },
      });
      assert.equal(decision.approved, false);
      assert.equal(decision.reason, "not today");
    }),
);

test(
  "fails closed on HTTP errors",
  () =>
    withEnv(ENV, async () => {
      const fetchImpl = async () => ({
        status: 500,
        async text() {
          return "{}";
        },
      });
      await assert.rejects(
        () =>
          authorize(
            approvalPayload({
              sessionId: "sess-1",
              toolCallId: "call-1",
              toolName: "bash",
              arguments: { command: "ls" },
            }),
            { fetchImpl },
          ),
        GovernanceError,
      );
    }),
);

test("appendAudit writes JSONL", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-"));
  const path = join(dir, "audit.jsonl");
  await withEnv({ ...ENV, MEZMO_GOVERNANCE_AUDIT_LOG: path }, async () => {
    await appendAudit({ agent: AGENT, tool_name: "bash", status: "observed" });
    const line = (await readFile(path, "utf8")).trim();
    assert.equal(JSON.parse(line).agent, "opencode");
  });
});
