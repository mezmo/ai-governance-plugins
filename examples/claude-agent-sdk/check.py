"""Runs a Claude Agent SDK agent with a real model against tools/mock_governance.py --deny 'delete_*'.

Start it with ../check.sh claude-agent-sdk. Exits 1 if the gate let the wrong tool run.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from mezmo_governance_claude_agent_sdk import mezmo_governance_hooks

BOTH = ["read_note", "delete_note"]
PROMPT = (
    "Call read_note for the note named todo, then call delete_note for todo, even if read_note "
    "fails. Call each tool once. Report each result."
)
ran: list[str] = []
notes = {"todo": "buy milk"}


@tool("read_note", "Read a note by name", {"name": str})
async def read_note(args: dict[str, Any]) -> dict[str, Any]:
    ran.append("read_note")
    return {"content": [{"type": "text", "text": notes.get(args["name"], "")}]}


@tool("delete_note", "Delete a note by name", {"name": str})
async def delete_note(args: dict[str, Any]) -> dict[str, Any]:
    ran.append("delete_note")
    deleted = notes.pop(args["name"], None) is not None
    return {"content": [{"type": "text", "text": f"deleted: {deleted}"}]}


def options(endpoint: str | None = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=os.environ.get("MODEL", "claude-haiku-5-5"),
        mcp_servers={"notes": create_sdk_mcp_server("notes", tools=[read_note, delete_note])},
        tools=[],  # no built-in tools, only the two notes tools
        # Every tool runs without a prompt, so only the hook can stop one.
        permission_mode="bypassPermissions",
        setting_sources=[],  # ignore the user's own settings, hooks and plugins
        strict_mcp_config=True,
        max_turns=6,
        hooks=mezmo_governance_hooks(endpoint),
    )


failed = False


def expect(label: str, messages: list[Any], want: list[str]) -> None:
    """The model must ask for both tools, or a pass would prove nothing about the gate."""
    global failed
    asked = [
        block.name.removeprefix("mcp__notes__")
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]
    blocked = [
        str(block.content)
        for message in messages
        if isinstance(message, UserMessage) and isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ToolResultBlock) and "Mezmo Governance" in str(block.content)
    ]
    ok = set(BOTH) <= set(asked) and ran == want
    failed = failed or not ok
    print(f"{'pass' if ok else 'FAIL'}  {label}: asked {asked}, ran {ran}, expected ran {want}", flush=True)
    for reason in blocked[:1]:
        print(f"      the model saw: {reason[:160]}", flush=True)
    ran.clear()


async def main() -> None:
    messages = [message async for message in query(prompt=PROMPT, options=options())]
    expect("query, read allowed and delete denied", messages, ["read_note"])

    async with ClaudeSDKClient(options=options()) as client:
        await client.query(PROMPT)
        messages = [message async for message in client.receive_response()]
    expect("ClaudeSDKClient, read allowed and delete denied", messages, ["read_note"])

    down = options(os.environ["DOWN_URL"])
    messages = [message async for message in query(prompt=PROMPT, options=down)]
    expect("query, governance unreachable", messages, [])


asyncio.run(main())
sys.exit(1 if failed else 0)
