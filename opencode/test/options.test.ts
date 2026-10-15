import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";

import plugin from "../mezmo-governance.ts";
import { auditLines, withFakeMezmo } from "./runtime.ts";

const OPTIONS_URL = "https://options.example.test/governance/workflow/authorize";
const BASE = { tool: "shell", sessionID: "ses_1", agent: "build", messageID: "msg_1", id: "call_1" };

type Sent = { url: string; authorization: string };

function recordingFetch(sent: Sent[]) {
  return async (url: string, init?: any) => {
    sent.push({ url, authorization: init?.headers?.Authorization });
    return { status: 200, text: async () => '{"approved":true}' };
  };
}

async function v2Hooks(options: Record<string, unknown>) {
  const hooks: Record<string, (event: any) => Promise<void>> = {};
  await plugin.setup({
    options,
    tool: {
      hook: async (name: string, callback: (event: any) => Promise<void>) => {
        hooks[name] = callback;
        return {};
      },
    },
  } as any);
  return hooks;
}

// OpenCode 2.x runs tools in a background service that does not inherit the
// shell that ran `opencode`, so plugin options must reach the gate.
test("2.x plugin options win over the environment", async () => {
  const sent: Sent[] = [];
  await withFakeMezmo(recordingFetch(sent), async (audit) => {
    const optionsAudit = join(audit, "..", "options-audit.jsonl");
    const hooks = await v2Hooks({
      MEZMO_GOVERNANCE_URL: OPTIONS_URL,
      MEZMO_GOVERNANCE_ACCESS_KEY: "from-options",
      MEZMO_GOVERNANCE_AUDIT_LOG: optionsAudit,
    });
    await hooks["execute.before"]({ ...BASE, input: { command: "ls" } });
    await hooks["execute.after"]({ ...BASE, input: { command: "ls" }, status: "completed", result: {} });
    assert.equal((await auditLines(optionsAudit)).length, 1);
  });
  assert.equal(sent[0].url, OPTIONS_URL);
  assert.equal(sent[0].authorization, "Token from-options");
});

test("1.x plugin options win over the environment", async () => {
  const sent: Sent[] = [];
  await withFakeMezmo(recordingFetch(sent), async () => {
    const hooks = await plugin.server({} as any, { MEZMO_GOVERNANCE_ACCESS_KEY: "from-options" });
    await hooks["tool.execute.before"](
      { tool: "bash", sessionID: "ses_1", callID: "call_1" },
      { args: { command: "ls" } },
    );
  });
  assert.equal(sent[0].authorization, "Token from-options");
  assert.match(sent[0].url, /^https:\/\/api\.example\.test\//);
});

test("without options the environment applies", async () => {
  const sent: Sent[] = [];
  await withFakeMezmo(recordingFetch(sent), async () => {
    const hooks = await v2Hooks({});
    await hooks["execute.before"]({ ...BASE, input: { command: "ls" } });
  });
  assert.equal(sent[0].authorization, "Token sts_test");
});

// A `{file:...}` or `{env:...}` reference that does not resolve becomes "".
// That must block, not fall back to some other key.
test("an empty option blocks instead of falling back to the environment", async () => {
  await withFakeMezmo(recordingFetch([]), async () => {
    const hooks = await v2Hooks({ MEZMO_GOVERNANCE_ACCESS_KEY: "" });
    await assert.rejects(
      hooks["execute.before"]({ ...BASE, input: { command: "ls" } }),
      /MEZMO_GOVERNANCE_ACCESS_KEY is required/,
    );
  });
});

// One 2.x service can run the plugin for two projects with different options.
test("two instances with different options never mix while calls overlap", async () => {
  const sent: Sent[] = [];
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const fetchImpl = async (url: string, init?: any) => {
    sent.push({ url, authorization: init?.headers?.Authorization });
    await gate;
    return { status: 200, text: async () => '{"approved":true}' };
  };
  await withFakeMezmo(fetchImpl, async () => {
    const first = await v2Hooks({ MEZMO_GOVERNANCE_ACCESS_KEY: "key-a" });
    const second = await v2Hooks({ MEZMO_GOVERNANCE_ACCESS_KEY: "key-b" });
    const calls = [
      first["execute.before"]({ ...BASE, input: { command: "ls" } }),
      second["execute.before"]({ ...BASE, id: "call_2", input: { command: "ls" } }),
    ];
    release();
    await Promise.all(calls);
  });
  assert.deepEqual(
    sent.map((item) => item.authorization),
    ["Token key-a", "Token key-b"],
  );
});
