import assert from "node:assert/strict";
import test from "node:test";

import { approvalPayload, authorizationHeader, authorize, GovernanceError } from "../mezmo-governance/client.ts";

const SECRET = "sts_secret";

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

function env(url: string, token = SECRET) {
  return {
    MEZMO_GOVERNANCE_URL: url,
    MEZMO_GOVERNANCE_TOKEN: token,
    MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "5",
    MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
  };
}

const payload = () =>
  approvalPayload({ sessionId: "sess-1", toolCallId: "call-1", toolName: "bash", arguments: { command: "ls" } });

function approving(calls: string[]) {
  return async (url: string) => {
    calls.push(url);
    return {
      status: 200,
      async text() {
        return JSON.stringify({ approved: true });
      },
    };
  };
}

// Without fetchImpl the real fetch builds the header; its error quotes the value.
test("a token no header can hold is refused with only the variable name", async () => {
  for (const token of [`${SECRET}\nX-Injected: 1`, `${SECRET} two`, `${SECRET}é`, `Bearer ${SECRET}\u0001`]) {
    await withEnv(env("http://127.0.0.1:1/authorize", token), async () => {
      await assert.rejects(authorize(payload()), (error: Error) => {
        assert.ok(error instanceof GovernanceError);
        assert.equal(error.message, "MEZMO_GOVERNANCE_TOKEN must be printable ASCII without spaces");
        return true;
      });
    });
  }
});

test("a token with a Token or Bearer scheme keeps working", () =>
  withEnv(env("https://api.example.test/authorize"), async () => {
    assert.equal(authorizationHeader(), `Token ${SECRET}`);
    process.env.MEZMO_GOVERNANCE_TOKEN = `Bearer ${SECRET}`;
    assert.equal(authorizationHeader(), `Bearer ${SECRET}`);
  }));

test("a failed request gives a fixed reason that quotes neither token nor URL", () =>
  withEnv(env("https://api.example.test/authorize"), async () => {
    const fetchImpl = async () => {
      throw new Error(`connect to https://user:pass@api.example.test failed with Token ${SECRET}`);
    };
    await assert.rejects(authorize(payload(), { fetchImpl }), (error: Error) => {
      assert.ok(error instanceof GovernanceError);
      assert.equal(error.message, "Mezmo Governance is unavailable");
      return true;
    });
  }));

test("plain http to another host is refused before the token is sent", () =>
  withEnv(env("http://api.example.test/authorize"), async () => {
    const calls: string[] = [];
    await assert.rejects(authorize(payload(), { fetchImpl: approving(calls) }), (error: Error) => {
      assert.ok(error instanceof GovernanceError);
      assert.equal(error.message, "MEZMO_GOVERNANCE_URL must use https, or http to this machine");
      return true;
    });
    assert.deepEqual(calls, []);
  }));

test("https and plain http to this machine keep working", async () => {
  for (const url of [
    "https://api.example.test/authorize",
    "http://127.0.0.1:8790/authorize",
    "http://localhost:8790/authorize",
    "http://[::1]:8790/authorize",
  ]) {
    await withEnv(env(url), async () => {
      const calls: string[] = [];
      assert.deepEqual(await authorize(payload(), { fetchImpl: approving(calls) }), {
        approved: true,
        reason: null,
      });
      assert.deepEqual(calls, [url]);
    });
  }
});
