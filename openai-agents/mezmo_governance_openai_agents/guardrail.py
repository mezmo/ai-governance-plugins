"""OpenAI Agents SDK tool input guardrail for Mezmo exact-tool-call governance."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agents import (
    Agent,
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    FunctionTool,
    LocalShellTool,
    ShellTool,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
)
from agents.tracing import get_current_trace

from mezmo_governance_openai_agents.client import (
    GovernanceError,
    HttpJson,
    approval_payload,
    authorize,
    httpx_json,
)

GUARDRAIL_NAME = "mezmo_governance"
# These run on this machine, but the SDK runs no tool input guardrail for them.
UNGATED_LOCAL_TOOLS = (ComputerTool, ShellTool, LocalShellTool, ApplyPatchTool, CustomTool)
# The SDK's id for a run with tracing turned off: every run shares it.
NO_OP_TRACE_ID = "no-op"


def session_id_from_context(context: Any, default: str) -> str:
    """Return RunConfig.group_id, else the run's trace id, else ``default``."""
    run_config = getattr(context, "run_config", None)
    group_id = getattr(run_config, "group_id", None)
    if isinstance(group_id, str) and group_id.strip():
        return group_id.strip()
    current = get_current_trace()
    trace_id = getattr(current, "trace_id", None)
    if isinstance(trace_id, str) and trace_id.strip() and trace_id != NO_OP_TRACE_ID:
        return trace_id
    return default


def tool_arguments(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise GovernanceError("tool arguments must be a JSON string")
    try:
        # The SDK runs a tool called with empty arguments as {}.
        arguments = json.loads(raw) if raw else {}
    except ValueError as error:
        raise GovernanceError("tool arguments are not valid JSON") from error
    if not isinstance(arguments, Mapping):
        raise GovernanceError("tool arguments must be a JSON object")
    return arguments


class MezmoGovernance:
    """Ask Mezmo Governance before every function and MCP tool of an agent runs."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        access_key: str | None = None,
        matched_pattern: str = "*",
        http: Callable[
            [str, str, Mapping[str, Any] | None, float],
            Awaitable[tuple[int, dict[str, Any]]],
        ]
        | None = None,
    ) -> None:
        # Explicit arguments win over MEZMO_GOVERNANCE_URL and
        # MEZMO_GOVERNANCE_ACCESS_KEY, which are read per call when these are None.
        self.endpoint = endpoint
        self.access_key = access_key
        self.matched_pattern = matched_pattern
        self.http = http
        # Session for runs without a group_id or a trace: one per instance.
        self.session_id = f"openai-agents-{uuid.uuid4()}"

    def guardrail(self, namespace: str | None = None) -> ToolInputGuardrail[Any]:
        """A guardrail for ``tool_input_guardrails``; ``namespace`` names an MCP server."""

        async def mezmo_governance(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            # Any failure before a decision blocks the call. The model gets the
            # message in place of the tool result and the run keeps going.
            try:
                decision = await authorize(
                    self._payload(data, namespace),
                    http=self._transport(),
                    url=self.endpoint,
                )
            except Exception as exc:
                return ToolGuardrailFunctionOutput.reject_content(
                    "Mezmo Governance could not authorize this tool call, "
                    f"so the OpenAI Agents SDK blocked it: {exc}"
                )
            if not decision.approved:
                return ToolGuardrailFunctionOutput.reject_content(
                    "Mezmo Governance denied this tool call: "
                    f"{decision.reason or 'no reason provided'}"
                )
            return ToolGuardrailFunctionOutput.allow()

        return ToolInputGuardrail(guardrail_function=mezmo_governance, name=GUARDRAIL_NAME)

    def govern(self, agent: Agent[Any]) -> Agent[Any]:
        """Add the guardrail to every function tool and MCP server of ``agent``.

        Also governs handoff targets and agents wrapped with ``as_tool``. Raises
        GovernanceError for a local tool kind the SDK cannot guard, so it never
        runs ungated. Call it after the agent's tools are final.
        """
        self._govern(agent, set())
        return agent

    def _govern(self, agent: Agent[Any], seen: set[int]) -> None:
        if id(agent) in seen:
            return
        seen.add(id(agent))
        for tool in agent.tools:
            if isinstance(tool, UNGATED_LOCAL_TOOLS):
                raise GovernanceError(
                    f"{type(tool).__name__} {tool.name!r} on agent {agent.name!r} cannot be "
                    "gated by a tool input guardrail; remove it or gate it with needs_approval"
                )
            if isinstance(tool, FunctionTool):
                attach(tool, self.guardrail)
                nested = getattr(tool, "_agent_instance", None)
                if isinstance(nested, Agent):
                    self._govern(nested, seen)
        for server in agent.mcp_servers:
            attach(server, lambda: self.guardrail(namespace=server.name))
        for handoff in agent.handoffs:
            target = handoff if isinstance(handoff, Agent) else handoff_agent(handoff)
            if target is None:
                raise GovernanceError(
                    f"cannot find the agent behind handoff {handoff.agent_name!r}; "
                    "build it with handoff() so its tools can be gated"
                )
            self._govern(target, seen)

    def _payload(self, data: ToolInputGuardrailData, namespace: str | None) -> dict[str, Any]:
        context = data.context
        session_id = session_id_from_context(context, self.session_id)
        # A call without an id gets a random one, so it never reuses a decision.
        tool_call_id = str(getattr(context, "tool_call_id", "") or f"generated-{uuid.uuid4()}")
        payload = approval_payload(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=str(context.tool_name),
            arguments=tool_arguments(context.tool_arguments),
            matched_pattern=self.matched_pattern,
        )
        # An MCP server name, else a namespace the SDK gives the tool.
        namespace = namespace or getattr(context, "tool_namespace", None)
        if namespace:
            payload["items"][0]["tool_namespace"] = namespace
        return payload

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


def attach(owner: Any, make: Callable[[], ToolInputGuardrail[Any]]) -> None:
    """Add one Mezmo guardrail to a tool or MCP server, first in line, once."""
    guardrails = list(owner.tool_input_guardrails or [])
    if any(guardrail.get_name() == GUARDRAIL_NAME for guardrail in guardrails):
        return
    owner.tool_input_guardrails = [make(), *guardrails]


def handoff_agent(handoff: Any) -> Agent[Any] | None:
    reference = getattr(handoff, "_agent_ref", None)
    target = reference() if callable(reference) else None
    return target if isinstance(target, Agent) else None
