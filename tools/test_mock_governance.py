"""The mock server, driven by the real Claude Code hook over HTTP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mock_governance  # noqa: E402

HOOK = Path(__file__).parent.parent / "claude-code" / "scripts" / "governance_hook.py"
# The Claude Code plugin lets every call run while it has no key; the Codex hook denies.
KEYED_HOOK = Path(__file__).parent.parent / "codex" / "mezmo_governance_hook.py"


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
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.env = {
            **os.environ,
            "MEZMO_GOVERNANCE_URL": f"http://127.0.0.1:{self.server.server_port}{mock_governance.PATH}",
            "MEZMO_GOVERNANCE_TOKEN": "local-test",
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2",
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "0.2",
            "MEZMO_GOVERNANCE_STATE_DIR": state.name,
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def run_hook(self, payload: dict, hook: Path = HOOK) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(hook)],
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

    def test_held_call_answers_like_mezmo(self) -> None:
        # Mezmo answers 207 while a call waits for a person, on the POST and on each poll.
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        url = self.env["MEZMO_GOVERNANCE_URL"]
        headers = {"Authorization": "Token local-test", "Content-Type": "application/json"}

        def call(method: str, query: str = "", body: dict | None = None) -> tuple[int, dict]:
            data = json.dumps(body).encode() if body is not None else None
            try:
                with urlopen(Request(url + query, data=data, headers=headers, method=method)) as reply:
                    return reply.status, json.loads(reply.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        post = {"decision_id": "d1", "items": [{"tool_name": "Write"}]}
        self.assertEqual(call("POST", body=post)[0], 207)
        self.assertEqual(call("POST", body=post)[0], 409)
        self.assertEqual(call("GET", "?decision_id=d1"), (207, {"approved": False, "reason": None}))
        self.assertEqual(call("POST", body={"decision_id": "d2", "items": [{"tool_name": "Read"}]})[0], 200)
        self.assertEqual(call("GET", "?decision_id=d2"), (200, {"approved": True, "reason": None}))

    def test_arguments_are_not_printed(self) -> None:
        import contextlib
        import io

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            payload = event("Bash", "c6")
            payload["tool_input"] = {"command": "export API_KEY=CANARY_SECRET"}
            self.run_hook(payload)
        self.assertIn("denied", captured.getvalue())
        self.assertNotIn("CANARY_SECRET", captured.getvalue())

    def test_each_decision_is_one_write(self) -> None:
        # Parallel calls run on parallel threads; a line split over two writes interleaves.
        import contextlib

        class Writes:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def write(self, text: str) -> int:
                self.calls.append(text)
                return len(text)

            def flush(self) -> None:
                pass

        writes = Writes()
        with contextlib.redirect_stdout(writes):
            self.run_hook(event("Read", "c7"))
        self.assertEqual(writes.calls, ["approved claude_code:Read\n"])

    def test_missing_key_is_rejected(self) -> None:
        self.env.pop("MEZMO_GOVERNANCE_TOKEN")
        result = self.run_hook(event("Read", "c5"), KEYED_HOOK)
        self.assertEqual(self.decision(result), "deny")


class CheckScriptTests(unittest.TestCase):
    def test_exit_status_survives_cleanup(self) -> None:
        # Stopping the mock must not replace the script's own exit status.
        script = Path(__file__).parent.parent / "examples" / "check.sh"
        result = subprocess.run(["sh", str(script), "no-such-example"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == "__main__":
    unittest.main()
