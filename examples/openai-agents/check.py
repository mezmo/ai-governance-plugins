"""Runs an OpenAI Agents SDK agent with a real model against tools/mock_governance.py --deny 'delete_*'.

Start it with ../check.sh openai-agents. Exits 1 if the gate let the wrong tool run.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner, function_tool
from agents.items import ToolCallItem
from openai import AsyncOpenAI

from mezmo_governance_openai_agents import MezmoGovernance

BOTH = ["read_note", "delete_note"]
PROMPT = "Call read_note for the note named todo, then call delete_note for todo. Report each result."
# Tracing off keeps tool arguments out of the OpenAI traces dashboard.
RUN_CONFIG = RunConfig(group_id="examples-check", tracing_disabled=True)
ran: list[str] = []
notes = {"todo": "buy milk"}


def read_note(name: str) -> str:
    """Read a note by name."""
    ran.append("read_note")
    return notes.get(name, "")


def delete_note(name: str) -> bool:
    """Delete a note by name."""
    ran.append("delete_note")
    return notes.pop(name, None) is not None


def agent(endpoint: str | None = None) -> Agent:
    # Fresh tools per agent: govern() adds its guardrail to each tool once.
    tools = [function_tool(read_note), function_tool(delete_note)]
    # A client per agent: run_sync and asyncio.run each use their own event loop,
    # and the SDK's shared default client stays bound to the first one.
    model = OpenAIResponsesModel(model=os.environ.get("MODEL", "gpt-5-mini"), openai_client=AsyncOpenAI())
    return MezmoGovernance(endpoint).govern(Agent(name="notes", model=model, tools=tools))


failed = False


def expect(label: str, result, want: list[str]) -> None:
    """The model must ask for both tools, or a pass would prove nothing about the gate."""
    global failed
    asked = [item.raw_item.name for item in result.new_items if isinstance(item, ToolCallItem)]
    ok = set(BOTH) <= set(asked) and ran == want
    failed = failed or not ok
    print(f"{'pass' if ok else 'FAIL'}  {label}: asked {asked}, ran {ran}, expected ran {want}", flush=True)
    ran.clear()


expect("run_sync, read allowed and delete denied", Runner.run_sync(agent(), PROMPT, run_config=RUN_CONFIG), ["read_note"])


async def run_async() -> None:
    streamed = Runner.run_streamed(agent(), PROMPT, run_config=RUN_CONFIG)
    async for _event in streamed.stream_events():
        pass
    expect("run_streamed, read allowed and delete denied", streamed, ["read_note"])
    down = await Runner.run(agent(os.environ["DOWN_URL"]), PROMPT, run_config=RUN_CONFIG)
    expect("run, governance unreachable", down, [])


asyncio.run(run_async())
sys.exit(1 if failed else 0)
