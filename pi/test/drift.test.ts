import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

// pi, opencode, mastra and claude-agent-sdk each ship their own client.ts so a user can copy one
// directory. The copies may differ only in the agent name, origin.kind and the
// default origin.reason. This check needs the whole repository; a copied
// directory skips it.
const REPO = dirname(dirname(dirname(fileURLToPath(import.meta.url))));
const COPIES = [
  "pi/client.ts",
  "opencode/mezmo-governance/client.ts",
  "mastra/client.ts",
  "claude-agent-sdk/typescript/client.ts",
];

function normalized(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/^export const AGENT = ".*";$/m, 'export const AGENT = "<agent>";')
    .replace(/^ {6}kind: "[a-z_]+",$/m, '      kind: "<origin>",')
    .replace(/^ {6}reason: params\.eventName \?\? ".*",$/m, '      reason: params.eventName ?? "<event>",');
}

test("client.ts copies differ only in agent, origin.kind and default reason", (t) => {
  const paths = COPIES.map((copy) => join(REPO, copy));
  const missing = paths.filter((path) => !existsSync(path));
  if (missing.length > 0) {
    t.skip(`sibling adapters not present: ${missing.join(", ")}`);
    return;
  }
  const [first, ...rest] = paths.map(normalized);
  assert.ok(first.includes('"<agent>"') && first.includes('"<origin>"') && first.includes('"<event>"'));
  rest.forEach((other, index) => {
    assert.equal(other, first, `${COPIES[index + 1]} drifted from ${COPIES[0]}`);
  });
});
