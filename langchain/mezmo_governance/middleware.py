"""LangChain middleware for Mezmo exact-tool-call governance."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from types import TracebackType
from typing import Any, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from mezmo_governance.client import (
    GovernanceError,
    HttpJson,
    approval_payload,
    authorize,
    httpx_json,
)

CURRENT_TURN_ID: ContextVar[str | None] = ContextVar(
    "mezmo_governance_turn_id",
    default=None,
)

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # optional
    trace = None
    Status = None  # type: ignore[misc, assignment]
    StatusCode = None  # type: ignore[misc, assignment]

TRACE_OPERATION = "mcp.tool_call"
T = TypeVar("T")


def set_current_turn_id(turn_id: str) -> Token[str | None]:
    return CURRENT_TURN_ID.set(turn_id)


def reset_current_turn_id(token: Token[str | None]) -> None:
    CURRENT_TURN_ID.reset(token)


def session_id_from_request(request: ToolCallRequest, default: str) -> str:
    """Return the LangGraph thread_id, else the current turn id, else ``default``."""
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None)
    if isinstance(config, dict):
        configurable = config.get("configurable") or {}
        if isinstance(configurable, dict):
            thread_id = configurable.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
    turn_id = CURRENT_TURN_ID.get()
    if turn_id:
        return turn_id
    return default


def refuse_provider_mcp(tools: list[Any]) -> None:
    """Stop the run when the model provider would call an MCP server itself.

    The provider runs such a tool, so no tool call passes this middleware.
    """
    for tool in tools:
        if isinstance(tool, Mapping) and str(tool.get("type", "")).startswith("mcp"):
            raise GovernanceError(
                f"Mezmo Governance cannot gate a {tool['type']!r} tool the model provider runs; "
                "connect the MCP server with langchain-mcp-adapters instead"
            )


def tool_result_text(result: ToolMessage | Command) -> str:
    if isinstance(result, ToolMessage):
        content = result.content
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(result)


class MezmoGovernanceMiddleware(AgentMiddleware):
    """Apply Mezmo Governance before every LangChain tool call."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        access_key: str | None = None,
        matched_pattern: str = "*",
        intent_provider: Callable[[ToolCallRequest], str | None] | None = None,
        http: Callable[
            [str, str, Mapping[str, Any] | None, float],
            Awaitable[tuple[int, dict[str, Any]]],
        ]
        | None = None,
    ) -> None:
        super().__init__()
        # Explicit arguments win over MEZMO_GOVERNANCE_URL and
        # MEZMO_GOVERNANCE_TOKEN, which are read per call when these are None.
        self.endpoint = endpoint
        # Session for calls without a thread_id or turn id: one per middleware.
        self.session_id = f"middleware-{uuid.uuid4()}"
        self.matched_pattern = matched_pattern
        self.access_key = access_key
        self.intent_provider = intent_provider
        self.http = http

    async def __aenter__(self) -> MezmoGovernanceMiddleware:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def _payload(self, request: ToolCallRequest) -> dict[str, Any]:
        call = request.tool_call
        arguments = call["args"]
        if not isinstance(arguments, Mapping):
            raise GovernanceError("tool arguments must be a JSON object")
        session_id = session_id_from_request(request, self.session_id)
        # A call without an id gets a random one, so it never reuses a decision.
        tool_call_id = str(call.get("id") or f"generated-{uuid.uuid4()}")
        intent = self.intent_provider(request) if self.intent_provider else None
        return approval_payload(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=str(call["name"]),
            arguments=arguments,
            matched_pattern=self.matched_pattern,
            intent=intent,
        )

    def _transport(self) -> HttpJson:
        if self.http is not None:
            return self.http
        access_key = self.access_key

        async def http(
            method: str,
            url: str,
            payload_body: Mapping[str, Any] | None,
            timeout: float,
        ) -> tuple[int, dict[str, Any]]:
            return await httpx_json(method, url, payload_body, timeout, access_key=access_key)

        return http

    @staticmethod
    def _blocked(request: ToolCallRequest, message: str) -> ToolMessage:
        return ToolMessage(
            content=message,
            tool_call_id=str(request.tool_call.get("id") or ""),
            status="error",
        )

    async def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        """Return None when Mezmo approves, else the error ToolMessage to send back."""
        # Any failure before a decision blocks the call. The agent sees an error
        # ToolMessage and keeps running, as with a deny.
        try:
            decision = await authorize(
                self._payload(request),
                http=self._transport(),
                url=self.endpoint,
            )
        except Exception as exc:
            return self._unavailable(request, exc)
        if not decision.approved:
            return self._blocked(
                request,
                "Mezmo Governance denied this tool call: "
                f"{decision.reason or 'no reason provided'}",
            )
        return None

    def _unavailable(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        return self._blocked(
            request,
            f"Mezmo Governance could not authorize this tool call, so LangChain blocked it: {exc}",
        )

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], Any]) -> Any:
        refuse_provider_mcp(request.tools)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[Any]],
    ) -> Any:
        refuse_provider_mcp(request.tools)
        return await handler(request)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            blocked = run_sync(self._check(request))
        except Exception as exc:
            blocked = self._unavailable(request, exc)
        if blocked is not None:
            return blocked
        call = request.tool_call
        with traced(str(call["name"]), call["args"]) as span:
            return finish(span, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command],
        ],
    ) -> ToolMessage | Command:
        blocked = await self._check(request)
        if blocked is not None:
            return blocked
        call = request.tool_call
        with traced(str(call["name"]), call["args"]) as span:
            return finish(span, await handler(request))


def run_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, in a worker thread if a loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = copy_context()  # keeps CURRENT_TURN_ID visible in the worker
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coroutine).result()


@contextmanager
def traced(tool_name: str, arguments: Mapping[str, Any]) -> Iterator[Any]:
    """Wrap an approved call in an `mcp.tool_call` span when OpenTelemetry is installed."""
    if trace is None:
        yield None
        return
    tracer = trace.get_tracer("mezmo.governance.langchain")
    with tracer.start_as_current_span(TRACE_OPERATION) as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute(
            "tool.parameters",
            json.dumps(
                dict(arguments),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            if Status is not None and StatusCode is not None:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def finish(span: Any, result: ToolMessage | Command) -> ToolMessage | Command:
    if span is not None:
        span.set_attribute("tool.result", tool_result_text(result)[:8000])
        if Status is not None and StatusCode is not None:
            span.set_status(Status(StatusCode.OK))
    return result
