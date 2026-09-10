#!/usr/bin/env python3
"""Minimal LangChain agent gated by Mezmo Governance."""

from __future__ import annotations

import asyncio
import os
import uuid

from langchain.agents import create_agent
from langchain.messages import AIMessage
from langchain.tools import tool
from mezmo_governance.middleware import (
    MezmoGovernanceMiddleware,
    reset_current_turn_id,
    set_current_turn_id,
)


@tool
def echo(text: str) -> str:
    """Return the provided text unchanged."""
    return text


async def interactive() -> int:
    middleware = MezmoGovernanceMiddleware(
        os.environ.get("MEZMO_GOVERNANCE_URL"),
        access_key=os.environ.get("MEZMO_GOVERNANCE_ACCESS_KEY"),
        matched_pattern="*",
    )
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
    agent = create_agent(
        model=model,
        tools=[echo],
        middleware=[middleware],
        system_prompt=(
            "You are a small demo agent. Use the echo tool when you need to "
            "repeat user text. A denied tool call is final for that request."
        ),
    )
    session_id = f"langchain:{uuid.uuid4()}"
    print(f"langchain-agent · MezmoGovernanceMiddleware · {model}")
    print(f"session {session_id}\n")
    messages: list = []
    while True:
        try:
            prompt = input("❯ ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return 0
        token = set_current_turn_id(session_id)
        try:
            result = await agent.ainvoke(
                {"messages": [*messages, {"role": "user", "content": prompt}]},
                config={"configurable": {"thread_id": session_id}},
            )
            messages = list(result["messages"])
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    content = message.content
                    print(f"\n{content if isinstance(content, str) else content}\n")
                    break
        finally:
            reset_current_turn_id(token)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(interactive()))
