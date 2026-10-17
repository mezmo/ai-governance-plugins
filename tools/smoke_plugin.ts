// Run one tool call through an installed OpenCode 2.x or Pi plugin, the way
// the agent calls it. `./mezmo-governance smoke` runs this with node.
//
//   node tools/smoke_plugin.ts <opencode|pi> <plugin .ts file> <tool name> < options.json
//
// OpenCode gets the JSON object on stdin as its plugin options. It prints the decision as one JSON line and exits 2 on deny, 0 on approve.
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

type Handler = (event: unknown, ctx?: unknown) => Promise<any> | any;

const [kind, file, tool] = process.argv.slice(2);
const plugin = (await import(pathToFileURL(file).href)).default;
const call = `smoke-${randomUUID()}`;
const input = { command: "echo hi" };
let blocked: string | undefined;

if (kind === "opencode") {
  // OpenCode 2.x: setup registers execute.before, which throws to block.
  const hooks: Record<string, Handler> = {};
  await plugin.setup({
    options: JSON.parse(readFileSync(0, "utf8") || "{}"),
    tool: {
      hook: async (name: string, callback: Handler) => {
        hooks[name] = callback;
        return { dispose: async () => {} };
      },
    },
  });
  try {
    await hooks["execute.before"]({ tool, sessionID: "smoke", agent: "build", messageID: "smoke", id: call, input });
  } catch (error) {
    blocked = error instanceof Error ? error.message : String(error);
  }
} else {
  // Pi: the tool_call handler returns { block, reason } to block.
  const handlers = new Map<string, Handler>();
  plugin({ on: (event: string, handler: Handler) => handlers.set(event, handler) });
  const ctx = { sessionManager: { getSessionId: () => "smoke" } };
  const result = await handlers.get("tool_call")!({ type: "tool_call", toolCallId: call, toolName: tool, input }, ctx);
  blocked = result?.block ? String(result.reason) : undefined;
}

console.log(JSON.stringify(blocked === undefined ? { decision: "approve" } : { decision: "deny", reason: blocked }));
process.exit(blocked === undefined ? 0 : 2);
