import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";

import { approvalPayload, authorize, GovernanceError } from "../client.ts";

const PAYLOAD = approvalPayload({
  sessionId: "sess-1",
  toolCallId: "call-1",
  toolName: "bash",
  arguments: { command: "ls" },
});

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

const ENV = {
  MEZMO_GOVERNANCE_URL: "https://api.example.test/governance/workflow/authorize",
  MEZMO_GOVERNANCE_TOKEN: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

function reply(status: number, body: unknown) {
  return { status, text: async () => JSON.stringify(body) };
}

// A fetch that ignores its abort signal stands in for a response that lands
// after the timer: the decision must still be refused.
test("an approval that arrives after the deadline is refused", () =>
  withEnv(ENV, async () => {
    let clock = 0;
    const fetchImpl = async () => {
      clock += 31_000;
      return reply(200, { approved: true });
    };
    await assert.rejects(authorize(PAYLOAD, { fetchImpl, now: () => clock }), GovernanceError);
  }));

test("a poll approval that arrives after the deadline is refused", () =>
  withEnv(ENV, async () => {
    let clock = 0;
    const fetchImpl = async (_url: string, init?: { method?: string }) => {
      if (init?.method === "POST") {
        return reply(409, {});
      }
      clock += 31_000;
      return reply(200, { status: "approved" });
    };
    await assert.rejects(
      authorize(PAYLOAD, { fetchImpl, now: () => clock, sleep: async () => {} }),
      GovernanceError,
    );
  }));

// The timeout covers the whole body, not each read: a server that sends one
// byte at a time cannot hold the call open past the deadline.
test("a server that drips its body past the deadline is refused", async () => {
  const body = '{"approved":true}' + " ".repeat(40);
  const server = createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json", "Content-Length": body.length });
    let sent = 0;
    const timer = setInterval(() => {
      if (sent >= body.length || response.destroyed) {
        clearInterval(timer);
        response.end();
        return;
      }
      response.write(body[sent]);
      sent += 1;
    }, 50);
    response.on("close", () => clearInterval(timer));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await withEnv(
      {
        ...ENV,
        MEZMO_GOVERNANCE_URL: `http://127.0.0.1:${port}/governance/workflow/authorize`,
        MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "0.5",
      },
      async () => {
        const started = Date.now();
        await assert.rejects(authorize(PAYLOAD), GovernanceError);
        assert.ok(Date.now() - started < 2000, `took ${Date.now() - started} ms`);
      },
    );
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});
