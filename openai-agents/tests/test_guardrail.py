"""Tests for MezmoGovernance. Skipped when the OpenAI Agents SDK is not installed."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mezmo_governance_openai_agents.client import call_digest  # noqa: E402

try:
    from agents import Agent, RunConfig, Runner, ShellTool, function_tool, handoff
    from agents.items import ToolCallOutputItem
    from agents.mcp import MCPServer
    from agents.testing import ScriptedModel, assistant_message, function_call
    from mcp.types import CallToolResult, TextContent
    from mcp.types import Tool as MCPTool
except ImportError:
    Agent = None  # type: ignore[misc, assignment]
    MCPServer = object  # type: ignore[misc, assignment]

ENV = {
    "MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize",
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}
# Tracing off keeps runs offline; group_id gives them a stable session.
RUN_CONFIG: Any = None if Agent is None else RunConfig(group_id="conv-1", tracing_disabled=True)


class RecordingTransport:
    """Stands in for httpx_json: answers in turn and records what was sent."""

    def __init__(self, *responses: tuple[int, dict[str, Any]] | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, method: str, url: str, payload: Any, timeout: float) -> tuple[int, dict[str, Any]]:
        self.calls.append({"method": method, "url": url, "payload": payload})
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


ran: list[str] = []


def read_note_impl(name: str) -> str:
    """Read a note by name."""
    ran.append("read_note")
    return f"note {name}"


def delete_note_impl(name: str) -> bool:
    """Delete a note by name."""
    ran.append("delete_note")
    return True


def note_agent(governance: Any) -> Any:
    """A fresh agent whose scripted model asks for read_note once, then answers."""
    tools = [
        function_tool(read_note_impl, name_override="read_note"),
        function_tool(delete_note_impl, name_override="delete_note"),
    ]
    model = ScriptedModel(
        [
            [function_call("read_note", {"name": "todo"}, call_id="call-1")],
            [assistant_message("done")],
        ]
    )
    return governance.govern(Agent(name="notes", model=model, tools=tools))


def tool_outputs(result: Any) -> list[str]:
    return [str(item.output) for item in result.new_items if isinstance(item, ToolCallOutputItem)]


def guardrail_data(tool_call_id: str = "call-1", arguments: str = '{"name": "todo"}') -> Any:
    context = SimpleNamespace(
        tool_name="read_note",
        tool_call_id=tool_call_id,
        tool_arguments=arguments,
        tool_namespace=None,
        run_config=None,
    )
    return SimpleNamespace(context=context, agent=None)


@unittest.skipUnless(Agent is not None, "openai-agents is not installed")
class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        ran.clear()
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agent(self, http: Any = None, endpoint: str | None = None) -> Any:
        from mezmo_governance_openai_agents import MezmoGovernance

        agent = note_agent(MezmoGovernance(endpoint, http=http))
        return Runner.run_sync(agent, "read todo", run_config=RUN_CONFIG)

    def test_approved_call_runs_and_sends_the_exact_call(self) -> None:
        http = RecordingTransport((200, {"approved": True}))
        result = self.run_agent(http)
        self.assertEqual(ran, ["read_note"])
        self.assertEqual(tool_outputs(result), ["note todo"])
        payload = http.calls[0]["payload"]
        digest = call_digest("openai_agents", "read_note", {"name": "todo"})
        self.assertEqual(payload["request_id"], "openai_agents:conv-1:call-1#" + digest)
        self.assertEqual(payload["scope"], {"kind": "single", "session_id": "conv-1"})
        self.assertEqual(
            payload["items"],
            [{"id": 0, "tool_namespace": "openai_agents", "tool_name": "read_note", "arguments": {"name": "todo"}}],
        )

    def test_denied_call_does_not_run_and_the_model_sees_the_reason(self) -> None:
        http = RecordingTransport((200, {"approved": False, "reason": "nope"}))
        result = self.run_agent(http)
        self.assertEqual(ran, [])
        self.assertEqual(tool_outputs(result), ["Mezmo Governance denied this tool call: nope"])
        self.assertEqual(result.final_output, "done")

    def test_unreachable_mezmo_blocks_the_call(self) -> None:
        down = f"http://127.0.0.1:{free_port()}/governance/workflow/authorize"
        result = self.run_agent(endpoint=down)
        self.assertEqual(ran, [])
        self.assertIn("could not authorize this tool call", tool_outputs(result)[0])
        self.assertIn("unavailable", tool_outputs(result)[0])

    def test_missing_configuration_blocks_the_call(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_URL": ""}):
            result = self.run_agent(RecordingTransport((200, {"approved": True})))
        self.assertEqual(ran, [])
        self.assertIn("MEZMO_GOVERNANCE_URL must be a non-empty string", tool_outputs(result)[0])

    def test_a_reply_it_cannot_read_blocks_the_call(self) -> None:
        cases = [
            ((200, {"reason": "no field"}),),
            ((200, {"approved": "true"}),),
            ((500, {}),),
            ((409, {}), (200, {"status": "maybe"})),
        ]
        for responses in cases:
            with self.subTest(responses=responses):
                ran.clear()
                result = self.run_agent(RecordingTransport(*responses))
                self.assertEqual(ran, [])
                self.assertIn("could not authorize this tool call", tool_outputs(result)[0])

    def test_a_decision_still_pending_at_the_deadline_blocks_the_call(self) -> None:
        http = RecordingTransport((409, {}), (200, {"status": "pending", "reason": None}))
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "0.2"}):
            result = self.run_agent(http)
        self.assertEqual(ran, [])
        self.assertIn("approval still pending", tool_outputs(result)[0])
        self.assertEqual(http.calls[1]["method"], "GET")

    def test_an_exception_in_the_transport_blocks_the_call(self) -> None:
        result = self.run_agent(RecordingTransport(RuntimeError("boom")))
        self.assertEqual(ran, [])
        self.assertIn("could not authorize this tool call", tool_outputs(result)[0])
        self.assertIn("boom", tool_outputs(result)[0])

    def test_a_crash_in_the_guardrail_itself_stops_the_run_before_the_tool(self) -> None:
        # Outside the guardrail's own try block the SDK takes over: it raises
        # and the tool still does not run.
        class Broken:
            @staticmethod
            def allow() -> Any:
                raise RuntimeError("guardrail crashed")

        with patch("mezmo_governance_openai_agents.guardrail.ToolGuardrailFunctionOutput", Broken):
            with self.assertRaisesRegex(Exception, "guardrail crashed"):
                self.run_agent(RecordingTransport((200, {"approved": True})))
        self.assertEqual(ran, [])

    def test_runner_run_and_run_streamed_are_gated(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        async def both() -> list[Any]:
            denied = MezmoGovernance(http=RecordingTransport((200, {"approved": False, "reason": "nope"})))
            ran_result = await Runner.run(note_agent(denied), "read todo", run_config=RUN_CONFIG)
            streamed = Runner.run_streamed(note_agent(denied), "read todo", run_config=RUN_CONFIG)
            async for _event in streamed.stream_events():
                pass
            return [ran_result, streamed]

        for result in asyncio.run(both()):
            self.assertEqual(tool_outputs(result), ["Mezmo Governance denied this tool call: nope"])
        self.assertEqual(ran, [])

    def test_session_falls_back_to_one_id_per_instance(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": True}))
        governance = MezmoGovernance(http=http)
        for _ in range(2):
            Runner.run_sync(note_agent(governance), "read todo", run_config=RunConfig(tracing_disabled=True))
        sessions = {call["payload"]["scope"]["session_id"] for call in http.calls}
        self.assertEqual(sessions, {governance.session_id})


@unittest.skipUnless(Agent is not None, "openai-agents is not installed")
class GuardrailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def check(self, governance: Any, data: Any) -> Any:
        return await governance.guardrail().run(data)

    async def test_a_call_without_an_id_never_reuses_a_decision(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": True}))
        governance = MezmoGovernance(http=http)
        for _ in range(2):
            output = await self.check(governance, guardrail_data(tool_call_id=""))
            self.assertEqual(output.behavior["type"], "allow")
        first, second = (call["payload"] for call in http.calls)
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertTrue(first["request_id"].startswith(f"openai_agents:{governance.session_id}:generated-"))

    async def test_arguments_that_are_not_a_json_object_block_without_asking(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": True}))
        governance = MezmoGovernance(http=http)
        for arguments, message in (("{not json", "not valid JSON"), ("[1]", "must be a JSON object")):
            with self.subTest(arguments=arguments):
                output = await self.check(governance, guardrail_data(arguments=arguments))
                self.assertEqual(output.behavior["type"], "reject_content")
                self.assertIn(message, output.behavior["message"])
        self.assertEqual(http.calls, [])

    async def test_empty_arguments_are_sent_as_an_empty_object(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": True}))
        await self.check(MezmoGovernance(http=http), guardrail_data(arguments=""))
        self.assertEqual(http.calls[0]["payload"]["items"][0]["arguments"], {})


class FakeMCPServer(MCPServer):  # type: ignore[misc, valid-type]
    def __init__(self) -> None:
        super().__init__()
        self.called: list[str] = []

    @property
    def name(self) -> str:
        return "files"

    async def connect(self) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[Any]:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        return [MCPTool(name="delete_file", inputSchema=schema)]

    async def call_tool(self, tool_name: str, arguments: Any, meta: Any = None) -> Any:
        self.called.append(tool_name)
        return CallToolResult(content=[TextContent(type="text", text="deleted")])

    async def list_prompts(self) -> Any:
        raise NotImplementedError

    async def get_prompt(self, name: str, arguments: Any = None) -> Any:
        raise NotImplementedError


@unittest.skipUnless(Agent is not None, "openai-agents is not installed")
class GovernTests(unittest.TestCase):
    def setUp(self) -> None:
        ran.clear()
        patcher = patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mcp_tools_are_gated_with_the_server_as_namespace(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": False, "reason": "nope"}))
        server = FakeMCPServer()
        model = ScriptedModel(
            [
                [function_call("delete_file", {"path": "/tmp/x"}, call_id="call-1")],
                [assistant_message("done")],
            ]
        )
        agent = MezmoGovernance(http=http).govern(Agent(name="files", model=model, mcp_servers=[server]))
        result = Runner.run_sync(agent, "delete it", run_config=RUN_CONFIG)
        self.assertEqual(server.called, [])
        self.assertEqual(tool_outputs(result), ["Mezmo Governance denied this tool call: nope"])
        item = http.calls[0]["payload"]["items"][0]
        self.assertEqual((item["tool_namespace"], item["tool_name"]), ("files", "delete_file"))

    def test_a_tool_in_an_sdk_namespace_uses_that_namespace(self) -> None:
        from agents import tool_namespace
        from mezmo_governance_openai_agents import MezmoGovernance

        http = RecordingTransport((200, {"approved": True}))
        tools = tool_namespace(
            name="notes_ns",
            description="Note tools.",
            tools=[function_tool(read_note_impl, name_override="read_note")],
        )
        model = ScriptedModel(
            [
                [function_call("read_note", {"name": "todo"}, call_id="call-1", namespace="notes_ns")],
                [assistant_message("done")],
            ]
        )
        agent = MezmoGovernance(http=http).govern(Agent(name="notes", model=model, tools=tools))
        Runner.run_sync(agent, "read todo", run_config=RUN_CONFIG)
        self.assertEqual(ran, ["read_note"])
        item = http.calls[0]["payload"]["items"][0]
        self.assertEqual((item["tool_namespace"], item["tool_name"]), ("notes_ns", "read_note"))

    def test_handoffs_and_agents_as_tools_are_governed(self) -> None:
        from mezmo_governance_openai_agents import MezmoGovernance
        from mezmo_governance_openai_agents.guardrail import GUARDRAIL_NAME

        nested_tool = function_tool(read_note_impl, name_override="read_note")
        handoff_tool = function_tool(delete_note_impl, name_override="delete_note")
        nested = Agent(name="nested", tools=[nested_tool])
        target = Agent(name="target", tools=[handoff_tool])
        built_tool = function_tool(read_note_impl, name_override="read_built")
        built = Agent(name="built", tools=[built_tool])
        main = Agent(
            name="main",
            tools=[nested.as_tool("ask_nested", "Ask nested.")],
            handoffs=[target, handoff(built)],
        )
        target.handoffs = [main]  # a cycle must not loop
        MezmoGovernance().govern(main)
        for tool in (main.tools[0], nested_tool, handoff_tool, built_tool):
            with self.subTest(tool=tool.name):
                names = [guardrail.get_name() for guardrail in tool.tool_input_guardrails]
                self.assertEqual(names, [GUARDRAIL_NAME])

    def test_an_agent_tool_whose_agent_it_cannot_find_is_refused(self) -> None:
        # govern reads the SDK's private _agent_instance; if a release drops it,
        # the inner agent's tools must not run ungated.
        from mezmo_governance_openai_agents import GovernanceError, MezmoGovernance

        inner = Agent(name="inner", tools=[function_tool(delete_note_impl)])
        wrapped = inner.as_tool("ask_inner", "Ask inner.")
        del wrapped._agent_instance
        with self.assertRaisesRegex(GovernanceError, "cannot find the agent behind tool 'ask_inner'"):
            MezmoGovernance().govern(Agent(name="main", tools=[wrapped]))

    def test_a_function_tool_without_the_agent_tool_marker_is_refused(self) -> None:
        # Without _is_agent_tool, govern cannot tell a plain tool from one that
        # runs another agent, so it refuses every function tool.
        from agents import FunctionTool
        from mezmo_governance_openai_agents import GovernanceError, MezmoGovernance

        tool = function_tool(read_note_impl, name_override="read_note")
        del tool._is_agent_tool
        marker = FunctionTool.__dict__["_is_agent_tool"]
        delattr(FunctionTool, "_is_agent_tool")
        try:
            with self.assertRaisesRegex(GovernanceError, "cannot tell whether tool 'read_note' runs an agent"):
                MezmoGovernance().govern(Agent(name="main", tools=[tool]))
        finally:
            setattr(FunctionTool, "_is_agent_tool", marker)

    def test_a_handoff_whose_agent_it_cannot_find_is_refused(self) -> None:
        from agents import Handoff
        from mezmo_governance_openai_agents import GovernanceError, MezmoGovernance

        async def invoke(context: Any, arguments: str) -> Any:
            return Agent(name="hidden", tools=[function_tool(delete_note_impl)])

        hidden = Handoff(
            tool_name="transfer_to_hidden",
            tool_description="Hand off.",
            input_json_schema={},
            on_invoke_handoff=invoke,
            agent_name="hidden",
        )
        with self.assertRaisesRegex(GovernanceError, "cannot find the agent behind handoff 'hidden'"):
            MezmoGovernance().govern(Agent(name="main", handoffs=[hidden]))

    def test_govern_adds_the_guardrail_once_and_first(self) -> None:
        from agents import ToolInputGuardrail, ToolGuardrailFunctionOutput
        from mezmo_governance_openai_agents import MezmoGovernance
        from mezmo_governance_openai_agents.guardrail import GUARDRAIL_NAME

        def mine(data: Any) -> Any:
            return ToolGuardrailFunctionOutput.allow()

        tool = function_tool(read_note_impl, name_override="read_note")
        tool.tool_input_guardrails = [ToolInputGuardrail(guardrail_function=mine)]
        agent = Agent(name="notes", tools=[tool])
        MezmoGovernance().govern(agent)
        MezmoGovernance().govern(agent)
        names = [guardrail.get_name() for guardrail in tool.tool_input_guardrails]
        self.assertEqual(names, [GUARDRAIL_NAME, "mine"])

    def test_a_local_tool_it_cannot_gate_is_refused(self) -> None:
        from mezmo_governance_openai_agents import GovernanceError, MezmoGovernance

        shell = ShellTool(executor=lambda request: "ran")
        agent = Agent(name="shell", tools=[function_tool(read_note_impl, name_override="read_note"), shell])
        with self.assertRaisesRegex(GovernanceError, "ShellTool 'shell' on agent 'shell' cannot be gated"):
            MezmoGovernance().govern(agent)


if __name__ == "__main__":
    unittest.main()
