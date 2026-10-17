import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { callDigest, MAX_TIMEOUT_SECONDS, type FetchLike } from "../client.ts";

// The identity of the fixture call, mcp__notes__delete_note on { name: "todo" }.
const NOTE_DIGEST = callDigest("mcp__notes__delete_note", { name: "todo" });
import {
  createMezmoGovernanceHooks,
  PRE_TOOL_USE_TIMEOUT_SECONDS,
  type HookInput,
} from "../mezmo-governance.ts";

const URL = "https://api.example.test/governance/workflow/authorize";
const SIGNAL = { signal: new AbortController().signal };

function withEnv(values: Record<string, string | undefined>, fn: () => Promise<void>) {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
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

// A PreToolUse input as the SDK sends it for a custom SDK MCP tool.
function preEvent(overrides: Record<string, unknown> = {}): HookInput {
  return {
    hook_event_name: "PreToolUse",
    session_id: "sess-1",
    tool_name: "mcp__notes__delete_note",
    tool_input: { name: "todo" },
    tool_use_id: "toolu_01",
    ...overrides,
  } as HookInput;
}

type Seen = { method: string; url: string; body: Record<string, unknown> | null };

function fakeFetch(answers: { status: number; body: string }[]): { fetchImpl: FetchLike; seen: Seen[] } {
  const seen: Seen[] = [];
  const fetchImpl: FetchLike = async (url, init) => {
    seen.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(init.body) : null });
    const answer = answers.length > 1 ? answers.shift()! : answers[0];
    return { status: answer.status, text: async () => answer.body };
  };
  return { fetchImpl, seen };
}

function reasonOf(output: unknown): string {
  const specific = (output as { hookSpecificOutput: Record<string, string> }).hookSpecificOutput;
  assert.equal(specific.hookEventName, "PreToolUse");
  assert.equal(specific.permissionDecision, "deny");
  return specific.permissionDecisionReason;
}

function pre(fetchImpl?: FetchLike) {
  return createMezmoGovernanceHooks({ fetchImpl }).PreToolUse[0].hooks[0];
}

test("an approved call returns empty output and sends the protocol payload", () =>
  withEnv(ENV, async () => {
    const { fetchImpl, seen } = fakeFetch([{ status: 200, body: '{"approved": true}' }]);
    assert.deepEqual(await pre(fetchImpl)(preEvent(), "toolu_01", SIGNAL), {});
    assert.equal(seen[0].method, "POST");
    const body = seen[0].body!;
    assert.equal(body.request_id, `claude_agent_sdk:sess-1:toolu_01#${NOTE_DIGEST}`);
    assert.deepEqual(body.origin, { kind: "claude_agent_sdk_hooks", reason: "PreToolUse" });
    assert.deepEqual(body.items, [
      { id: 0, tool_namespace: "notes", tool_name: "delete_note", arguments: { name: "todo" } },
    ]);
  }));

test("a denied call carries Mezmo's reason", () =>
  withEnv(ENV, async () => {
    const { fetchImpl } = fakeFetch([{ status: 200, body: '{"approved": false, "reason": "deletes need a person"}' }]);
    const output = await pre(fetchImpl)(preEvent(), "toolu_01", SIGNAL);
    assert.equal(reasonOf(output), "Mezmo Governance denied this exact tool call: deletes need a person");
  }));

test("a decision still pending at the deadline is denied", () =>
  withEnv({ ...ENV, MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "1" }, async () => {
    const { fetchImpl, seen } = fakeFetch([
      { status: 409, body: "{}" },
      { status: 200, body: '{"status": "pending", "reason": null}' },
    ]);
    const output = await pre(fetchImpl)(preEvent(), "toolu_01", SIGNAL);
    assert.match(reasonOf(output), /approval still pending/);
    assert.deepEqual(seen.slice(0, 2).map((s) => s.method), ["POST", "GET"]);
  }));

test("a call without a tool use id gets a random one each time", () =>
  withEnv(ENV, async () => {
    const { fetchImpl, seen } = fakeFetch([{ status: 200, body: '{"approved": true}' }]);
    const hook = pre(fetchImpl);
    const event = preEvent({ tool_use_id: undefined });
    await hook(event, undefined, SIGNAL);
    await hook(event, undefined, SIGNAL);
    assert.match(String(seen[0].body!.request_id), /^claude_agent_sdk:sess-1:generated-/);
    assert.notEqual(seen[0].body!.decision_id, seen[1].body!.decision_id);
  }));

test("malformed input is denied without asking Mezmo", () =>
  withEnv(ENV, async () => {
    const cases: [unknown, RegExp][] = [
      ["not an object", /hook input must be a JSON object/],
      [preEvent({ tool_input: "rm -rf /" }), /tool arguments must be a JSON object/],
      [preEvent({ session_id: "" }), /session_id must be a non-empty string/],
      [preEvent({ tool_name: undefined }), /tool_name must be a non-empty string/],
    ];
    for (const [event, message] of cases) {
      const { fetchImpl, seen } = fakeFetch([{ status: 200, body: '{"approved": true}' }]);
      assert.match(reasonOf(await pre(fetchImpl)(event as HookInput, "toolu_01", SIGNAL)), message);
      assert.equal(seen.length, 0);
    }
  }));

test("missing settings deny", () =>
  withEnv({ MEZMO_GOVERNANCE_URL: undefined, MEZMO_GOVERNANCE_TOKEN: undefined }, async () => {
    assert.match(reasonOf(await pre()(preEvent(), "toolu_01", SIGNAL)), /MEZMO_GOVERNANCE_URL is required/);
  }));

async function serve(answers: { status: number; body: string }[]): Promise<Server> {
  const server = createServer((request, response) => {
    request.resume();
    request.on("end", () => {
      const answer = answers.shift()!;
      response.writeHead(answer.status, { "Content-Type": "application/json" });
      response.end(answer.body);
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

function urlOf(server: Server): string {
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}/governance/workflow/authorize`;
}

test("unreachable Mezmo denies", async () => {
  const server = await serve([]);
  const url = urlOf(server);
  await new Promise((resolve) => server.close(resolve));
  await withEnv({ ...ENV, MEZMO_GOVERNANCE_URL: url }, async () => {
    const reason = reasonOf(await pre()(preEvent(), "toolu_01", SIGNAL));
    assert.match(reason, /could not authorize this exact tool call/);
    assert.match(reason, /Mezmo Governance is unavailable/);
  });
});

test("a reply it cannot read denies", async () => {
  const cases: [{ status: number; body: string }[], RegExp][] = [
    [[{ status: 200, body: "<html>bad gateway</html>" }], /invalid JSON response/],
    [[{ status: 200, body: "[]" }], /non-object response/],
    [[{ status: 200, body: '{"approved": "true"}' }], /did not return an approval decision/],
    [[{ status: 500, body: "{}" }], /returned HTTP 500/],
    [[{ status: 409, body: "{}" }, { status: 200, body: '{"status": "maybe"}' }], /unexpected status/],
    [[{ status: 409, body: "{}" }, { status: 200, body: "{}" }], /unexpected status/],
    [[{ status: 409, body: "{}" }, { status: 200, body: '{"approved": "yes"}' }], /unexpected status/],
  ];
  for (const [answers, message] of cases) {
    const server = await serve(answers);
    try {
      await withEnv({ ...ENV, MEZMO_GOVERNANCE_URL: urlOf(server) }, async () => {
        assert.match(reasonOf(await pre()(preEvent(), "toolu_01", SIGNAL)), message);
      });
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  }
});

test("every tool event is registered without a matcher, and the hook outlasts the deadline", () => {
  const hooks = createMezmoGovernanceHooks();
  assert.deepEqual(Object.keys(hooks).sort(), ["PostToolUse", "PostToolUseFailure", "PreToolUse"]);
  for (const matchers of Object.values(hooks)) {
    assert.equal(matchers.length, 1);
    assert.equal("matcher" in matchers[0], false);
    assert.equal(matchers[0].hooks.length, 1);
  }
  assert.equal(hooks.PreToolUse[0].timeout, PRE_TOOL_USE_TIMEOUT_SECONDS);
  assert.ok(MAX_TIMEOUT_SECONDS < PRE_TOOL_USE_TIMEOUT_SECONDS);
});

test("post-tool hooks write one audit line and never block", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mezmo-agent-sdk-"));
  const log = join(dir, "audit.jsonl");
  const post = createMezmoGovernanceHooks();
  await withEnv({ MEZMO_GOVERNANCE_AUDIT_LOG: log }, async () => {
    const ok = { ...preEvent({ hook_event_name: "PostToolUse" }), tool_response: { content: [] } };
    assert.deepEqual(await post.PostToolUse[0].hooks[0](ok, "toolu_01", SIGNAL), {});
    const failed = { ...preEvent({ hook_event_name: "PostToolUseFailure" }), error: "no such note" };
    await post.PostToolUseFailure[0].hooks[0](failed, "toolu_02", SIGNAL);
    await post.PostToolUse[0].hooks[0](preEvent({ hook_event_name: "PostToolUse", tool_use_id: undefined }), undefined, SIGNAL);
  });
  const [first, second, third] = (await readFile(log, "utf8")).trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(first.request_id, `claude_agent_sdk:sess-1:toolu_01#${NOTE_DIGEST}`);
  assert.deepEqual([first.tool_namespace, first.tool_name, first.status], ["notes", "delete_note", "observed"]);
  assert.deepEqual([second.status, second.result_preview], ["failed", '"no such note"']);
  assert.equal(third.decision_id, null);

  const blocker = join(dir, "file");
  await writeFile(blocker, "");
  await withEnv({ MEZMO_GOVERNANCE_AUDIT_LOG: join(blocker, "audit.jsonl") }, async () => {
    const errors: string[] = [];
    const original = console.error;
    console.error = (message: string) => errors.push(message);
    try {
      assert.deepEqual(await post.PostToolUse[0].hooks[0](preEvent({ hook_event_name: "PostToolUse" }), "toolu_03", SIGNAL), {});
    } finally {
      console.error = original;
    }
    assert.match(errors.join("\n"), /could not write the local audit log/);
  });
});
