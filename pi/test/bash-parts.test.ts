import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { AGENT, approvalPayload, bashItems } from "../client.ts";

// Claude Code's parser writes these cases, so they define correct output. This check needs the
// whole repository; a copied directory skips it.
const CASES = join(dirname(fileURLToPath(import.meta.url)), "../../claude-code/tests/bash_parts_cases.json");

type Case = {
  input: Record<string, unknown>;
  tool_name: string;
  command_field: string;
  items: Record<string, unknown>[] | null;
};

test("splits shell commands as Claude Code does", (t) => {
  if (!existsSync(CASES)) {
    t.skip("claude-code/tests/bash_parts_cases.json not present");
    return;
  }
  const cases = JSON.parse(readFileSync(CASES, "utf8")) as Case[];
  assert.ok(cases.length > 0);
  for (const { input, tool_name, command_field, items } of cases) {
    const built = bashItems(input, tool_name, command_field);
    const actual = built?.map(({ tool_namespace: _namespace, ...item }) => item) ?? null;
    assert.deepEqual(actual, items, JSON.stringify(input));
  }
});

test("a shell call goes as parts only when its command field is given", () => {
  const call = {
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command: "cd /repo && rm -rf build" },
  };
  const whole = approvalPayload(call);
  const parts = approvalPayload({ ...call, shellCommandField: "command" });
  assert.deepEqual(whole.items, [{ id: 0, tool_namespace: AGENT, tool_name: "bash", arguments: call.arguments }]);
  assert.deepEqual(parts.items, [
    {
      id: 0,
      tool_namespace: AGENT,
      tool_name: "bash",
      arguments: { full_command: "cd /repo && rm -rf build", command: "cd /repo", program: "cd", arg_1: "/repo" },
    },
    {
      id: 1,
      tool_namespace: AGENT,
      tool_name: "bash",
      arguments: { command: "rm -rf build", program: "rm", arg_1: "build", flag_r: true, flag_f: true },
      dependencies: [0],
    },
  ]);
  assert.equal(parts.decision_id, whole.decision_id);
  assert.equal(parts.request_id, whole.request_id);
});

test("an MCP tool goes whole even with a command field", () => {
  const payload = approvalPayload({
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "mcp__box__bash",
    arguments: { command: "cd /repo && rm -rf build" },
    shellCommandField: "command",
  });
  assert.equal(payload.items.length, 1);
});

test("a shell call sent whole still leaves out the parser's field names", () => {
  const call = {
    sessionId: "sess-1",
    toolCallId: "call-1",
    toolName: "bash",
    arguments: { command: "cd /repo && rm -rf build", program: "ls", arg_1: "x", flag_f: true, full_command: "ls" },
  };
  const whole = approvalPayload({ ...call, shellCommandField: "command", splitShellCommand: false });
  assert.deepEqual(whole.items, [
    { id: 0, tool_namespace: AGENT, tool_name: "bash", arguments: { command: "cd /repo && rm -rf build" } },
  ]);
  assert.equal(whole.request_id, approvalPayload(call).request_id);
  // Any other call keeps its input as it is.
  assert.deepEqual(approvalPayload(call).items[0].arguments, call.arguments);
  const mcp = approvalPayload({ ...call, toolName: "mcp__box__bash", shellCommandField: "command" });
  assert.deepEqual(mcp.items[0].arguments, call.arguments);
});
