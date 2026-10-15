"""Tests for MezmoGovernanceMiddleware. Skipped when LangChain is not installed."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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


def tool_request(call_id: str | None = "call-1", thread_id: str | None = "thread-1") -> Any:
    config = {"configurable": {"thread_id": thread_id}} if thread_id else {}
    return ToolCallRequest(
        tool_call={"name": "echo", "args": {"text": "hi"}, "id": call_id},
        tool=None,
        state={},
        runtime=SimpleNamespace(config=config),
    )


class RecordingTransport:
    """Stands in for httpx_json and records what the middleware sent."""

    def __init__(self, response: tuple[int, dict[str, Any]] | BaseException) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        payload: Any,
        timeout: float,
        *,
        access_key: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {"method": method, "url": url, "payload": payload, "access_key": access_key}
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareConfigTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, ENV)
    async def test_constructor_endpoint_and_key_beat_the_environment(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        transport = RecordingTransport((200, {"approved": True}))
        middleware = MezmoGovernanceMiddleware(
            "https://other.example.test/authorize",
            access_key="sts_explicit",
        )

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="call-1")

        with patch("mezmo_governance.middleware.httpx_json", transport):
            await middleware.awrap_tool_call(tool_request(), handler)

        self.assertEqual(transport.calls[0]["url"], "https://other.example.test/authorize")
        self.assertEqual(transport.calls[0]["access_key"], "sts_explicit")

    async def test_constructor_does_not_change_the_environment(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        with patch.dict(os.environ, {}, clear=True):
            MezmoGovernanceMiddleware(
                "https://other.example.test/authorize",
                access_key="sts_explicit",
            )
            self.assertNotIn("MEZMO_GOVERNANCE_URL", os.environ)
            self.assertNotIn("MEZMO_GOVERNANCE_ACCESS_KEY", os.environ)

    @patch.dict(os.environ, ENV)
    async def test_poll_uses_the_constructor_endpoint(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        urls: list[str] = []

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            urls.append(url)
            if method == "POST":
                return 409, {}
            return 200, {"status": "approved", "reason": None}

        middleware = MezmoGovernanceMiddleware("https://other.example.test/authorize", http=http)

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="call-1")

        await middleware.awrap_tool_call(tool_request(), handler)
        self.assertTrue(all(url.startswith("https://other.example.test/authorize") for url in urls))
        self.assertIn("decision_id=", urls[1])


async def must_not_run(_request: Any) -> Any:
    raise AssertionError("handler must not run")


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def assert_blocked(self, middleware: Any, request: Any) -> None:
        result = await middleware.awrap_tool_call(request, must_not_run)
        self.assertIsInstance(result, ToolMessage)
        self.assertEqual(result.status, "error")
        self.assertIn("could not authorize", result.content)

    @patch.dict(os.environ, ENV)
    async def test_transport_exception_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            raise ConnectionError("connection refused")

        await self.assert_blocked(MezmoGovernanceMiddleware(http=http), tool_request())

    @patch.dict(os.environ, ENV)
    async def test_http_500_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 500, {}

        await self.assert_blocked(MezmoGovernanceMiddleware(http=http), tool_request())

    async def test_missing_configuration_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        with patch.dict(os.environ, {}, clear=True):
            await self.assert_blocked(MezmoGovernanceMiddleware(), tool_request())

    @patch.dict(os.environ, ENV)
    async def test_non_object_arguments_return_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        request = ToolCallRequest(
            tool_call={"name": "echo", "args": ["hi"], "id": "call-1"},
            tool=None,
            state={},
            runtime=SimpleNamespace(config={"configurable": {"thread_id": "t1"}}),
        )
        await self.assert_blocked(MezmoGovernanceMiddleware(http=http), request)

    @patch.dict(os.environ, ENV)
    async def test_failing_intent_provider_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        def intent(_request: Any) -> str:
            raise KeyError("intent")

        middleware = MezmoGovernanceMiddleware(http=http, intent_provider=intent)
        await self.assert_blocked(middleware, tool_request())


if __name__ == "__main__":
    unittest.main()
