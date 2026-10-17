"""Runs a LangChain agent with a real model against tools/mock_governance.py --deny 'delete_*'.

Start it with ../check.sh langchain. Exits 1 if the gate let the wrong tool run.
"""

from __future__ import annotations

import asyncio
import os
import sys

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from mezmo_governance.middleware import MezmoGovernanceMiddleware

BOTH = ["read_note", "delete_note"]
PROMPT = "Call read_note for the note named todo, then call delete_note for todo. Report each result."
ran: list[str] = []
notes = {"todo": "buy milk"}


@tool
def read_note(name: str) -> str:
    """Read a note by name."""
    ran.append("read_note")
    return notes.get(name, "")


@tool
def delete_note(name: str) -> bool:
    """Delete a note by name."""
    ran.append("delete_note")
    return notes.pop(name, None) is not None


def agent(endpoint: str | None = None):
    # ChatOpenAI reads OPENAI_API_KEY and OPENAI_BASE_URL; set both to use another provider.
    model = ChatOpenAI(model=os.environ.get("MODEL", "gpt-5-mini"))
    return create_agent(
        model=model,
        tools=[read_note, delete_note],
        middleware=[MezmoGovernanceMiddleware(endpoint)],
    )


failed = False


def expect(label: str, result: dict, want: list[str]) -> None:
    """The model must ask for both tools, or a pass would prove nothing about the gate."""
    global failed
    asked = [c["name"] for m in result["messages"] for c in getattr(m, "tool_calls", None) or []]
    ok = set(BOTH) <= set(asked) and ran == want
    failed = failed or not ok
    print(f"{'pass' if ok else 'FAIL'}  {label}: asked {asked}, ran {ran}, expected ran {want}", flush=True)
    ran.clear()


message = {"messages": [{"role": "user", "content": PROMPT}]}

expect("invoke, read allowed and delete denied", agent().invoke(message), ["read_note"])



async def run_async() -> None:
    # One event loop for both runs: langchain-openai caches its async client per loop.
    expect("ainvoke, read allowed and delete denied", await agent().ainvoke(message), ["read_note"])
    down = await agent(os.environ["DOWN_URL"]).ainvoke(message)
    expect("ainvoke, governance unreachable", down, [])


asyncio.run(run_async())
sys.exit(1 if failed else 0)
