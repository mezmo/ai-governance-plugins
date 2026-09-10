"""LangChain middleware for Mezmo exact-tool-call governance."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.messages import ToolMessage
from langchain.tools.tool_node import ToolCallRequest
from langgraph.types import Command

from mezmo_governance.client import (
    AGENT,
    GovernanceError,
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


class GovernanceBlocked(RuntimeError):
    """The tool call could not be authorized and was not executed."""


class DecisionIdStore:
    """Process-local stable IDs for retried middleware calls."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._decision_ids: dict[str, str] = {}

    async def get_or_create(self, request_id: str) -> str:
        async with self._lock:
            return self._decision_ids.setdefault(
                request_id,
                str(uuid.uuid5(uuid.NAMESPACE_URL, request_id)),
            )


def set_current_turn_id(turn_id: str) -> Token[str | None]:
    return CURRENT_TURN_ID.set(turn_id)


def reset_current_turn_id(token: Token[str | None]) -> None:
    CURRENT_TURN_ID.reset(token)


def session_id_from_request(request: ToolCallRequest) -> str:
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
    raise GovernanceBlocked("The current LangChain turn has no stable ID")


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
        decision_ids: DecisionIdStore | None = None,
        matched_pattern: str = "*",
        intent_provider: Callable[[ToolCallRequest], str | None] | None = None,
        http: Callable[
            [str, str, Mapping[str, Any] | None, float],
            Awaitable[tuple[int, dict[str, Any]]],
        ]
        | None = None,
    ) -> None:
        super().__init__()
        if endpoint:
            import os

            os.environ.setdefault("MEZMO_GOVERNANCE_URL", endpoint)
        self.matched_pattern = matched_pattern
        self.access_key = access_key
        self.decision_ids = decision_ids or DecisionIdStore()
        self.intent_provider = intent_provider
        self.http = http
        if access_key:
            import os

            os.environ.setdefault("MEZMO_GOVERNANCE_ACCESS_KEY", access_key)

    async def __aenter__(self) -> MezmoGovernanceMiddleware:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command],
        ],
    ) -> ToolMessage | Command:
        call = request.tool_call
        tool_name = str(call["name"])
        arguments = call["args"]
        tool_call_id = str(call["id"])

        if not isinstance(arguments, Mapping):
            raise GovernanceBlocked("Tool arguments are not a JSON object")

        try:
            session_id = session_id_from_request(request)
        except GovernanceBlocked:
            raise

        request_id = f"{AGENT}:{session_id}:{tool_call_id}"
        decision_id = await self.decision_ids.get_or_create(request_id)
        intent = self.intent_provider(request) if self.intent_provider else None
        payload = approval_payload(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            matched_pattern=self.matched_pattern,
            intent=intent,
            decision_id=decision_id,
        )

        http = self.http
        if http is None and self.access_key:

            async def http(
                method: str,
                url: str,
                payload_body: Mapping[str, Any] | None,
                timeout: float,
            ) -> tuple[int, dict[str, Any]]:
                return await httpx_json(
                    method,
                    url,
                    payload_body,
                    timeout,
                    access_key=self.access_key,
                )

        try:
            decision = await authorize(payload, http=http)
        except (GovernanceError, TimeoutError, ValueError) as exc:
            raise GovernanceBlocked(
                "Mezmo Governance could not authorize this call"
            ) from exc

        if not decision.approved:
            return ToolMessage(
                content=(
                    "Mezmo Governance denied this tool call: "
                    f"{decision.reason or 'no reason provided'}"
                ),
                tool_call_id=tool_call_id,
                status="error",
            )

        return await self._execute_with_trace(
            request,
            handler,
            tool_name,
            arguments,
        )

    @staticmethod
    async def _execute_with_trace(
        request: ToolCallRequest,
        handler: Callable[
            [ToolCallRequest],
            Awaitable[ToolMessage | Command],
        ],
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolMessage | Command:
        if trace is None:
            return await handler(request)

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
                result = await handler(request)
            except Exception as exc:
                span.record_exception(exc)
                if Status is not None and StatusCode is not None:
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise

            span.set_attribute("tool.result", tool_result_text(result)[:8000])
            if Status is not None and StatusCode is not None:
                span.set_status(Status(StatusCode.OK))
            return result
