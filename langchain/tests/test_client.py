"""Tests for the LangChain Mezmo Governance client."""

from __future__ import annotations

import os
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mezmo_governance.client import (  # noqa: E402
    GovernanceError,
    approval_payload,
    authorize,
    call_identity,
    poll_interval_seconds,
    split_tool_name,
    timeout_seconds,
)

ENV = {
    "MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize",
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


class FakeHttp:
    def __init__(self, responses: list[tuple[int, dict[str, Any]] | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None, float]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def test_split_mcp_names(self) -> None:
        self.assertEqual(
            split_tool_name("mcp__kubernetes_demo__pods_delete"),
            ("kubernetes_demo", "pods_delete"),
        )
        self.assertEqual(split_tool_name("echo"), ("langchain", "echo"))

    def test_identity_and_payload(self) -> None:
        decision_id, request_id = call_identity("sess-1", "call-1")
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
            matched_pattern="*",
        )
        self.assertEqual(request_id, "langchain:sess-1:call-1")
        self.assertEqual(payload["decision_id"], decision_id)
        self.assertEqual(payload["origin"]["kind"], "langchain_middleware")
        self.assertEqual(payload["origin"]["matched_pattern"], "*")
        self.assertEqual(payload["items"][0]["tool_namespace"], "langchain")

    def test_default_payload_has_only_protocol_fields(self) -> None:
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
            matched_pattern="*",
        )
        self.assertEqual(
            set(payload),
            {"version", "decision_id", "request_id", "scope", "origin", "items"},
        )
        self.assertEqual(
            set(payload["items"][0]),
            {"id", "tool_namespace", "tool_name", "arguments"},
        )

    def test_intent_is_sent_only_when_given(self) -> None:
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
            intent="say hi",
        )
        self.assertEqual(payload["items"][0]["tool_call_intent"], "say hi")

    @patch.dict(os.environ, ENV)
    async def test_allows(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
        )
        decision = await authorize(payload, http=http)
        self.assertTrue(decision.approved)
        self.assertEqual(http.calls[0][0], "POST")

    @patch.dict(os.environ, ENV)
    async def test_denies(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "nope"})])
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
        )
        decision = await authorize(payload, http=http)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "nope")

    @patch.dict(os.environ, ENV)
    async def test_polls_after_timeout(self) -> None:
        http = FakeHttp(
            [
                TimeoutError("held"),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "ok"}),
            ]
        )
        clock = FakeClock()
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
        )
        decision = await authorize(
            payload,
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(http.calls[1][0], "GET")

    @patch.dict(os.environ, ENV)
    async def test_polls_after_409(self) -> None:
        http = FakeHttp(
            [
                (409, {"code": "ECONFLICT"}),
                (200, {"status": "denied", "reason": "already decided"}),
            ]
        )
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
        )
        decision = await authorize(payload, http=http)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "already decided")

    @patch.dict(os.environ, ENV)
    async def test_http_error_fails_closed(self) -> None:
        http = FakeHttp([(500, {})])
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="echo",
            arguments={"text": "hi"},
        )
        with self.assertRaises(GovernanceError):
            await authorize(payload, http=http)


# Same bounds as the Python hooks: finish before a 600 s runtime kill, and
# never poll more than once a second.
MAX_TIMEOUT_SECONDS = 590.0
MIN_POLL_INTERVAL_SECONDS = 1.0
TIMEOUT = "MEZMO_GOVERNANCE_TIMEOUT_SECONDS"
POLL = "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS"


class DeadlineBoundsTests(unittest.TestCase):
    def seconds(self, name: str, configured: str) -> float:
        with patch.dict(os.environ, {**ENV, name: configured}):
            return timeout_seconds() if name == TIMEOUT else poll_interval_seconds()

    def test_timeout_at_or_above_the_hook_budget_is_capped(self) -> None:
        for configured in ("590", "600", "3600", "1e308"):
            with self.subTest(configured=configured):
                self.assertEqual(self.seconds(TIMEOUT, configured), MAX_TIMEOUT_SECONDS)

    def test_small_positive_timeouts_are_kept(self) -> None:
        for configured, expected in (("30", 30.0), ("5e-324", 5e-324)):
            with self.subTest(configured=configured):
                self.assertEqual(self.seconds(TIMEOUT, configured), expected)

    def test_rejects_seconds_that_are_not_finite_and_positive(self) -> None:
        for name in (TIMEOUT, POLL):
            for configured in ("0", "-0", "-1", "nan", "inf", "-inf", "", "ten"):
                with self.subTest(name=name, configured=configured):
                    with self.assertRaises(GovernanceError):
                        self.seconds(name, configured)

    def test_poll_interval_has_a_floor(self) -> None:
        for configured in ("5e-324", "0.001"):
            with self.subTest(configured=configured):
                self.assertEqual(self.seconds(POLL, configured), MIN_POLL_INTERVAL_SECONDS)
        self.assertEqual(self.seconds(POLL, "1e308"), 1e308)


if __name__ == "__main__":
    unittest.main()
