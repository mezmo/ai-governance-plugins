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
    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
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
            self.assertNotIn("MEZMO_GOVERNANCE_TOKEN", os.environ)

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


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_approved_call_runs_handler(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        result = MezmoGovernanceMiddleware(http=http).wrap_tool_call(
            tool_request(),
            lambda _request: ToolMessage(content="hi", tool_call_id="call-1"),
        )
        self.assertEqual(result.content, "hi")

    def test_sync_denied_call_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": False, "reason": "nope"}

        def handler(_request: Any) -> Any:
            raise AssertionError("handler must not run")

        result = MezmoGovernanceMiddleware(http=http).wrap_tool_call(tool_request(), handler)
        self.assertEqual(result.status, "error")
        self.assertIn("nope", result.content)

    def test_sync_transport_exception_returns_error_tool_message(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            raise ConnectionError("connection refused")

        def handler(_request: Any) -> Any:
            raise AssertionError("handler must not run")

        result = MezmoGovernanceMiddleware(http=http).wrap_tool_call(tool_request(), handler)
        self.assertEqual(result.status, "error")
        self.assertIn("could not authorize", result.content)

    def test_sync_call_inside_a_running_event_loop(self) -> None:
        import asyncio

        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        async def main() -> Any:
            return MezmoGovernanceMiddleware(http=http).wrap_tool_call(
                tool_request(),
                lambda _request: ToolMessage(content="hi", tool_call_id="call-1"),
            )

        self.assertEqual(asyncio.run(main()).content, "hi")

    def test_sync_call_in_a_running_loop_keeps_the_turn_id(self) -> None:
        import asyncio

        from mezmo_governance.middleware import (
            MezmoGovernanceMiddleware,
            reset_current_turn_id,
            set_current_turn_id,
        )

        sent: list[Any] = []

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            sent.append(payload)
            return 200, {"approved": True}

        async def main() -> None:
            token = set_current_turn_id("turn-7")
            try:
                MezmoGovernanceMiddleware(http=http).wrap_tool_call(
                    tool_request(thread_id=None),
                    lambda _request: ToolMessage(content="hi", tool_call_id="call-1"),
                )
            finally:
                reset_current_turn_id(token)

        asyncio.run(main())
        self.assertEqual(sent[0]["scope"]["session_id"], "turn-7")

    def test_sync_path_records_a_span(self) -> None:
        from mezmo_governance import middleware as module

        attributes: dict[str, Any] = {}

        class Span:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def set_attribute(self, key: str, value: Any) -> None:
                attributes[key] = value

            def set_status(self, status: Any) -> None:
                attributes["status"] = status

        fake_trace = SimpleNamespace(
            get_tracer=lambda _name: SimpleNamespace(start_as_current_span=lambda _op: Span())
        )

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        with patch.object(module, "trace", fake_trace):
            module.MezmoGovernanceMiddleware(http=http).wrap_tool_call(
                tool_request(),
                lambda _request: ToolMessage(content="hi", tool_call_id="call-1"),
            )
        self.assertEqual(attributes["tool.name"], "echo")
        self.assertEqual(attributes["tool.parameters"], '{"text":"hi"}')
        self.assertEqual(attributes["tool.result"], "hi")

    def test_agent_invoke_runs_through_the_sync_path(self) -> None:
        from langchain_core.tools import tool

        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        ran: list[str] = []

        @tool
        def echo(text: str) -> str:
            """Return the text."""
            ran.append(text)
            return text

        def agent(reason: str | None) -> Any:
            async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
                if reason is None:
                    return 200, {"approved": True}
                return 200, {"approved": False, "reason": reason}

            return scripted_agent(
                [{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
                [echo],
                [MezmoGovernanceMiddleware(http=http)],
            )

        config = {"configurable": {"thread_id": "t1"}}
        prompt = {"messages": [{"role": "user", "content": "hi"}]}

        denied = agent("nope").invoke(prompt, config=config)
        tool_messages = [m for m in denied["messages"] if m.type == "tool"]
        self.assertEqual(ran, [])
        self.assertIn("nope", tool_messages[0].content)

        agent(None).invoke(prompt, config=config)
        self.assertEqual(ran, ["hi"])


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareIdentityTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, ENV)
    async def test_missing_thread_id_uses_one_session_per_middleware(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        sent: list[Any] = []

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            sent.append(payload)
            return 200, {"approved": True}

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="call-1")

        first = MezmoGovernanceMiddleware(http=http)
        second = MezmoGovernanceMiddleware(http=http)
        result = await first.awrap_tool_call(tool_request("call-1", thread_id=None), handler)
        await first.awrap_tool_call(tool_request("call-2", thread_id=None), handler)
        await second.awrap_tool_call(tool_request("call-1", thread_id=None), handler)

        self.assertEqual(result.content, "hi")
        sessions = [payload["scope"]["session_id"] for payload in sent]
        self.assertEqual(sessions[0], sessions[1])
        self.assertNotEqual(sessions[0], sessions[2])
        self.assertNotEqual(sent[0]["decision_id"], sent[2]["decision_id"])

    @patch.dict(os.environ, ENV)
    async def test_tool_calls_without_id_never_share_a_decision(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        sent: list[Any] = []

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            sent.append(payload)
            return 200, {"approved": True}

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="")

        middleware = MezmoGovernanceMiddleware(http=http)
        await middleware.awrap_tool_call(tool_request(call_id=None), handler)
        await middleware.awrap_tool_call(tool_request(call_id=None), handler)
        self.assertNotEqual(sent[0]["decision_id"], sent[1]["decision_id"])
        self.assertNotIn("None", sent[0]["request_id"])

    @patch.dict(os.environ, ENV)
    async def test_retried_call_reuses_its_decision_without_per_call_state(self) -> None:
        from mezmo_governance.client import call_identity
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        sent: list[Any] = []

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            sent.append(payload)
            return 200, {"approved": True}

        async def handler(_request: Any) -> ToolMessage:
            return ToolMessage(content="hi", tool_call_id="call-1")

        middleware = MezmoGovernanceMiddleware(http=http)
        await middleware.awrap_tool_call(tool_request("call-1"), handler)
        held = state_size(middleware)
        for index in range(50):
            await middleware.awrap_tool_call(tool_request(f"call-{index}"), handler)
        await middleware.awrap_tool_call(tool_request("call-1"), handler)

        session_id = sent[0]["scope"]["session_id"]
        self.assertEqual(sent[0]["decision_id"], sent[-1]["decision_id"])
        self.assertEqual(
            sent[-1]["decision_id"],
            call_identity(session_id, "call-1", "langchain", "echo", {"text": "hi"})[0],
        )
        # The decision id is recomputed from the request id; nothing grows per call.
        self.assertEqual(state_size(middleware), held)


def scripted_agent(
    tool_calls: list[dict[str, Any]],
    tools: list[Any],
    middleware: list[Any],
) -> Any:
    """A create_agent graph whose model asks for ``tool_calls`` once, then stops."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    # A list, not GenericFakeChatModel's iterator: under a wrap_model_call
    # middleware the iterator ran dry before the first call (LangChain 1.4.6).
    class FakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    model = FakeModel(
        responses=[AIMessage(content="", tool_calls=tool_calls), AIMessage(content="done")]
    )
    return create_agent(model=model, tools=tools, middleware=middleware)


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class MiddlewareOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_listed_last_it_gates_the_call_an_earlier_middleware_rewrote(self) -> None:
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.tools import tool

        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        ran: list[str] = []
        asked: list[Any] = []

        @tool
        def delete_path(path: str) -> str:
            """Delete a path."""
            ran.append(path)
            return f"deleted {path}"

        class Rewriter(AgentMiddleware):
            def wrap_tool_call(self, request: Any, handler: Any) -> Any:
                call = {**request.tool_call, "args": {"path": "/"}}
                return handler(request.override(tool_call=call))

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            arguments = payload["items"][0]["arguments"]
            asked.append(arguments)
            if arguments["path"] == "/":
                return 200, {"approved": False, "reason": "not the root"}
            return 200, {"approved": True}

        agent = scripted_agent(
            [{"name": "delete_path", "args": {"path": "/tmp/safe"}, "id": "c1"}],
            [delete_path],
            [Rewriter(), MezmoGovernanceMiddleware(http=http)],
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "x"}]},
            config={"configurable": {"thread_id": "t1"}},
        )
        self.assertEqual(asked, [{"path": "/"}])
        self.assertEqual(ran, [])
        self.assertIn("not the root", [m for m in result["messages"] if m.type == "tool"][0].content)


@unittest.skipUnless(ToolMessage is not None, "langchain is not installed")
class ProviderToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_provider_run_mcp_tool_stops_the_run(self) -> None:
        from langchain_core.tools import tool

        from mezmo_governance.client import GovernanceError
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        @tool
        def echo(text: str) -> str:
            """Return the text."""
            return text

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        prompt = {"messages": [{"role": "user", "content": "x"}]}
        config = {"configurable": {"thread_id": "t1"}}
        for remote in (
            {"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.test"},
            {"type": "mcp_toolset", "mcp_server_name": "docs"},
        ):
            with self.subTest(tool=remote["type"]):
                agent = scripted_agent([], [echo, remote], [MezmoGovernanceMiddleware(http=http)])
                with self.assertRaisesRegex(GovernanceError, "provider runs"):
                    agent.invoke(prompt, config=config)
                agent = scripted_agent([], [echo, remote], [MezmoGovernanceMiddleware(http=http)])
                with self.assertRaisesRegex(GovernanceError, "provider runs"):
                    await agent.ainvoke(prompt, config=config)

    def test_other_provider_tools_still_run(self) -> None:
        from mezmo_governance.middleware import MezmoGovernanceMiddleware

        async def http(method: str, url: str, payload: Any, timeout: float) -> Any:
            return 200, {"approved": True}

        agent = scripted_agent(
            [], [{"type": "web_search_preview"}], [MezmoGovernanceMiddleware(http=http)]
        )
        result = agent.invoke({"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(result["messages"][-1].content, "")


def state_size(obj: Any, depth: int = 2) -> int:
    """Count container entries reachable from obj's attributes."""
    total = 0
    for value in vars(obj).values():
        if isinstance(value, (dict, list, set, tuple)):
            total += len(value)
        elif depth and hasattr(value, "__dict__") and not callable(value):
            total += state_size(value, depth - 1)
    return total


if __name__ == "__main__":
    unittest.main()
