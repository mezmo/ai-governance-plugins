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
    split_tool_name,
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
    async def test_waits_for_a_person_after_207(self) -> None:
        http = FakeHttp(
            [
                (207, {"approved": False}),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "looked it over"}),
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
        self.assertEqual(decision.reason, "looked it over")
        poll_url = f"{ENV['MEZMO_GOVERNANCE_URL']}?decision_id={payload['decision_id']}"
        self.assertEqual(
            [(method, url) for method, url, _, _ in http.calls],
            [("POST", ENV["MEZMO_GOVERNANCE_URL"]), ("GET", poll_url), ("GET", poll_url)],
        )
        self.assertEqual(clock.now, 1.0)

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


if __name__ == "__main__":
    unittest.main()
