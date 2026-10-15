import assert from "node:assert/strict";
import test from "node:test";

import {
  GovernanceError,
  pollIntervalSeconds,
  timeoutSeconds,
} from "../client.ts";

const TIMEOUT = "MEZMO_GOVERNANCE_TIMEOUT_SECONDS";
const POLL = "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS";
// Same bounds as the Python hooks: finish before a 600 s runtime kill, and
// never poll more than once a second.
const MAX_TIMEOUT_SECONDS = 590;
const MIN_POLL_INTERVAL_SECONDS = 1;

function seconds(name: string, configured: string): number {
  const previous = process.env[name];
  process.env[name] = configured;
  try {
    return name === TIMEOUT ? timeoutSeconds() : pollIntervalSeconds();
  } finally {
    if (previous === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = previous;
    }
  }
}

test("timeout at or above the hook budget is capped", () => {
  for (const configured of ["590", "600", "3600", "1e308"]) {
    assert.equal(seconds(TIMEOUT, configured), MAX_TIMEOUT_SECONDS, configured);
  }
});

test("small positive timeouts are kept", () => {
  assert.equal(seconds(TIMEOUT, "30"), 30);
  assert.equal(seconds(TIMEOUT, "5e-324"), 5e-324);
});

test("rejects seconds that are not finite and positive", () => {
  for (const name of [TIMEOUT, POLL]) {
    for (const configured of ["0", "-0", "-1", "nan", "NaN", "inf", "Infinity", "-Infinity", "", "ten"]) {
      assert.throws(() => seconds(name, configured), GovernanceError, `${name}=${configured}`);
    }
  }
});

test("poll interval has a floor", () => {
  for (const configured of ["5e-324", "0.001"]) {
    assert.equal(seconds(POLL, configured), MIN_POLL_INTERVAL_SECONDS, configured);
  }
  assert.equal(seconds(POLL, "1e308"), 1e308);
});
