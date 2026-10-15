"""The mock server, driven by the real Claude Code hook over HTTP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mock_governance  # noqa: E402

HOOK = Path(__file__).parent.parent / "claude-code" / "mezmo_governance_hook.py"


def event(tool: str, call: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_use_id": call,
        "tool_name": tool,
        "tool_input": {"command": "ls"},
    }


class MockGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        handler = mock_governance.make_handler(["Bash", "kubernetes:*"], ["Write"])
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {
            **os.environ,
            "MEZMO_GOVERNANCE_URL": f"http://127.0.0.1:{self.server.server_port}{mock_governance.PATH}",
            "MEZMO_GOVERNANCE_ACCESS_KEY": "local-test",
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2",
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "0.2",
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def decision(self, result: subprocess.CompletedProcess) -> str | None:
        if not result.stdout.strip():
            return None
        return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]

    def test_matching_patterns(self) -> None:
        self.assertEqual(mock_governance.decide({"tool_name": "Bash"}, ["Bash"], []), "denied")
        item = {"tool_namespace": "kubernetes", "tool_name": "pods_delete"}
        self.assertEqual(mock_governance.decide(item, ["kubernetes:*"], []), "denied")
        self.assertEqual(mock_governance.decide(item, [], ["pods_*"]), "pending")
        self.assertEqual(mock_governance.decide({"tool_name": "Read"}, ["Bash"], []), "approved")

    def test_hook_allows_unmatched_tool(self) -> None:
        result = self.run_hook(event("Read", "c1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.decision(result))

    def test_hook_denies_matched_tool_with_reason(self) -> None:
        result = self.run_hook(event("Bash", "c2"))
        self.assertEqual(self.decision(result), "deny")
        self.assertIn("mock policy", result.stdout)

    def test_hook_denies_mcp_namespace(self) -> None:
        result = self.run_hook(event("mcp__kubernetes__pods_delete", "c3"))
        self.assertEqual(self.decision(result), "deny")

    def test_held_call_times_out_and_denies(self) -> None:
        result = self.run_hook(event("Write", "c4"))
        self.assertEqual(self.decision(result), "deny")
        self.assertIn("pending", result.stdout)

    def test_missing_key_is_rejected(self) -> None:
        self.env.pop("MEZMO_GOVERNANCE_ACCESS_KEY")
        self.env.pop("CLAUDE_PLUGIN_OPTION_ACCESS_KEY", None)
        result = self.run_hook(event("Read", "c5"))
        self.assertEqual(self.decision(result), "deny")


if __name__ == "__main__":
    unittest.main()
