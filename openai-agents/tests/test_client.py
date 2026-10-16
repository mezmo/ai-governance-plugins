"""Tests for the OpenAI Agents SDK copy of the Mezmo Governance client."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mezmo_governance_openai_agents.client import (  # noqa: E402
    approval_payload,
    call_digest,
    call_identity,
    split_tool_name,
)

CLIENT = ROOT / "mezmo_governance_openai_agents" / "client.py"
LANGCHAIN_CLIENT = ROOT.parent / "langchain" / "mezmo_governance" / "client.py"


def normalized(path: Path) -> str:
    """The client with the parts each copy may change replaced by placeholders."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'\A""".*"""\n', '"""<docstring>"""\n', text, count=1)
    text = re.sub(r'^AGENT = ".*"$', 'AGENT = "<agent>"', text, count=1, flags=re.M)
    text = re.sub(r'^( {8})"kind": "[a-z_]+",$', r'\1"kind": "<origin>",', text, count=1, flags=re.M)
    return re.sub(r'^( {8})"reason": "[a-z_]+",$', r'\1"reason": "<event>",', text, count=1, flags=re.M)


class ClientTests(unittest.TestCase):
    def test_identity_and_payload(self) -> None:
        decision_id, request_id = call_identity("sess-1", "call-1", "openai_agents", "read_note", {"name": "todo"})
        payload = approval_payload(
            session_id="sess-1",
            tool_call_id="call-1",
            tool_name="read_note",
            arguments={"name": "todo"},
            matched_pattern="*",
        )
        digest = call_digest("openai_agents", "read_note", {"name": "todo"})
        self.assertEqual(request_id, "openai_agents:sess-1:call-1#" + digest)
        self.assertEqual(payload["decision_id"], decision_id)
        self.assertEqual(
            payload["origin"],
            {"kind": "openai_agents_guardrail", "reason": "tool_input_guardrail", "matched_pattern": "*"},
        )
        self.assertEqual(payload["items"][0]["tool_namespace"], "openai_agents")
        self.assertEqual(split_tool_name("mcp__fs__read"), ("fs", "read"))


class DriftTests(unittest.TestCase):
    # This copy lets `pip install ./openai-agents` work without LangChain. It may
    # differ from langchain/ only in its docstring, agent name, origin.kind and
    # origin.reason, so the LangChain client tests cover its logic too.
    def test_client_matches_the_langchain_copy(self) -> None:
        if not LANGCHAIN_CLIENT.is_file():
            self.skipTest("langchain/ is not checked out next to this directory")
        mine = normalized(CLIENT)
        self.assertIn('"<agent>"', mine)
        self.assertIn('"<origin>"', mine)
        self.assertIn('"<event>"', mine)
        self.assertEqual(mine, normalized(LANGCHAIN_CLIENT), "client.py drifted from langchain/")


if __name__ == "__main__":
    unittest.main()
