import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";

import { approvalPayload, authorize, GovernanceError } from "../client.ts";

type Answer = { status: number; body: unknown; location?: string };

// A local server that replays scripted answers and records each request.
async function serve(answers: Answer[]) {
  const seen: IncomingMessage[] = [];
  const server: Server = createServer((request, response) => {
    seen.push(request);
    request.resume();
    const answer = answers.shift() ?? { status: 500, body: {} };
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (answer.location) {
      headers.Location = answer.location;
    }
    response.writeHead(answer.status, headers);
    response.end(JSON.stringify(answer.body));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return {
    seen,
    url: `http://127.0.0.1:${port}/governance/workflow/authorize`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

async function withEnv(values: Record<string, string>, fn: () => Promise<void>) {
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  try {
    await fn();
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
}

const cases: [string, Answer[], Answer][] = [
  ["authorize POST", [], { status: 200, body: { approved: true } }],
  ["status poll", [{ status: 409, body: {} }], { status: 200, body: { status: "approved" } }],
];

for (const [label, before, approval] of cases) {
  test(`a redirect on the ${label} denies and never sends the key elsewhere`, async () => {
    const elsewhere = await serve([approval]);
    const governance = await serve([...before, { status: 302, body: {}, location: elsewhere.url }]);
    try {
      await withEnv(
        {
          MEZMO_GOVERNANCE_URL: governance.url,
          MEZMO_GOVERNANCE_ACCESS_KEY: "sts_test",
          MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "10",
          MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
        },
        async () => {
          const payload = approvalPayload({
            sessionId: "sess-1",
            toolCallId: "call-1",
            toolName: "bash",
            arguments: { command: "ls" },
          });
          await assert.rejects(
            authorize(payload),
            (error: unknown) => error instanceof GovernanceError && error.message.includes("HTTP 302"),
          );
        },
      );
      assert.equal(governance.seen.length, before.length + 1);
      assert.deepEqual(elsewhere.seen.map((request) => request.headers.authorization), []);
    } finally {
      await elsewhere.close();
      await governance.close();
    }
  });
}
