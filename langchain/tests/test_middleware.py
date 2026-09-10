"""Tests for MezmoGovernanceMiddleware. Skipped when LangChain is not installed."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from langchain.messages import ToolMessage
    from langchain.tools.tool_node import ToolCallRequest
except ImportError:
    ToolMessage = None  # type: ignore[misc, assignment]
    ToolCallRequest = None  # type: ignore[misc, assignment]


ENV = {
    "MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize",
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_call_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(
            method: str,
            url: str,
            payload: Any,
            timeout: float,
        ) -> tuple[int, dict[str, Any]]:
            return 200, {"approved": False, "reason": "nope"}

        middleware = MezmoGovernanceMiddleware(http=http)
        request = ToolCallRequest(
            tool_call={"name": "echo", "args": {"text": "hi"}, "id": "call-1"},
            tool=None,
            state={},
            runtime=None,
        )

        async def handler(_request: Any) -> ToolMessage:
            raise AssertionError("handler must not run")

        with patch.dict(os.environ, ENV):
            from mezmo_governance.middleware import set_current_turn_id, reset_current_turn_id

            token = set_current_turn_id("sess-1")
            try:
                result = await middleware.awrap_tool_call(request, handler)
            finally:
                reset_current_turn_id(token)

        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("nope", result.content)

    @patch.dict(os.environ, ENV)
    async def test_approved_call_runs_handler(self) -> None:
        from mezmo_governance.middleware import (
            MezmoGovernanceMiddleware,
            reset_current_turn_id,
            set_current_turn_id,
        )

        async def http(
            method: str,
            url: str,
            payload: Any,
            timeout: float,
        ) -> tuple[int, dict[str, Any]]:
            return 200, {"approved": True}

        middleware = MezmoGovernanceMiddleware(http=http)
        request = ToolCallRequest(
            tool_call={"name": "echo", "args": {"text": "hi"}, "id": "call-1"},
            tool=None,
            state={},
            runtime=None,
        )

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="call-1")

        token = set_current_turn_id("sess-1")
        try:
            result = await middleware.awrap_tool_call(request, handler)
        finally:
            reset_current_turn_id(token)

        self.assertEqual(result.content, "hi")


if __name__ == "__main__":
    unittest.main()
