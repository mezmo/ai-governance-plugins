"""Tests for Codex's copy of ledger_investigation.py.

The Claude Code plugin's tests cover the CLI itself. Here: the copy matches it byte for byte, and
`launch` sends `opened` through mezmo_ledger_hook.py, the one path that differs. `launch` runs from
a temp folder holding the three scripts side by side, as installed, against an audit-service
defined here, with no setting of the developer's own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ADAPTER_DIR = Path(__file__).resolve().parent
CLI_PATH = ADAPTER_DIR / "ledger_investigation.py"
CLAUDE_CODE_CLI_PATH = ADAPTER_DIR.parent / "claude-code" / "scripts" / "ledger_investigation.py"
SCRIPTS = ("ledger_investigation.py", "mezmo_governance_hook.py", "mezmo_ledger_hook.py")

ACCOUNT = "test-acct"
# The epic's check value: account test-acct and this incident key.
INCIDENT_KEY = "pagerduty:Q34FCCWSTU9OI2"
CHECK_THREAD_ID = "ab2763da-3eea-5b82-a1bf-7922641cdf6c"


def serve(requests: list[Any], test: unittest.TestCase) -> str:
    """Start a server that accepts every request and keeps its body in `requests`. Returns its events URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            body = b'{"accepted":1}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}/v1/events"


class LedgerInvestigationTests(unittest.TestCase):
    def test_the_copy_matches_the_claude_code_cli(self) -> None:
        if not CLAUDE_CODE_CLI_PATH.is_file():
            self.skipTest("the Claude Code plugin is not checked out next to this one")
        self.assertEqual(CLI_PATH.read_bytes(), CLAUDE_CODE_CLI_PATH.read_bytes())

    def test_launch_sends_opened_with_the_codex_application_name(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        folder = Path(scratch.name)
        hooks = folder / "hooks"
        hooks.mkdir()
        for name in SCRIPTS:
            shutil.copy(ADAPTER_DIR / name, hooks / name)
        requests: list[Any] = []
        git_config = folder / "gitconfig"
        git_config.touch()
        # Built from nothing, so no setting of the developer's own reaches the script. Git reads
        # an empty config and no repository, so it reports no user email.
        env = {
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": str(folder / "no-git"),
            "HOME": str(folder),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(folder / "audit.jsonl"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(folder / "codex-sessions"),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(folder / "spool" / "ledger-spool.jsonl"),
            "MEZMO_LEDGER_URL": serve(requests, self),
            "NO_PROXY": "*",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        result = subprocess.run(
            [
                sys.executable, str(hooks / "ledger_investigation.py"), "launch", "--incident-key", INCIDENT_KEY, "--",
                sys.executable, "-c", "import os; print(os.environ['MEZMO_LEDGER_INVESTIGATION_ID'])",
            ],
            capture_output=True,
            check=False,
            # A folder outside any repository, so git finds no repository config either.
            cwd=folder,
            env=env,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{CHECK_THREAD_ID}\n")
        ((opened,),) = requests
        self.assertEqual(opened["actor"], {"account": ACCOUNT, "pk": "codex:launcher", "type": "agent"})
        self.assertEqual(opened["application"]["name"], "mezmo-codex-plugin")
        self.assertEqual((opened["entity_id"], opened["event"]), (CHECK_THREAD_ID, "agent.investigation.opened"))
        # Whole, with no session_id and, since git reports no email here, no operator_email.
        self.assertEqual(
            opened["extra"],
            {"agent": "codex", "incident_key": INCIDENT_KEY, "opened_by": "alert", "thread_id": CHECK_THREAD_ID},
        )
        self.assertFalse((folder / "spool").exists())


if __name__ == "__main__":
    unittest.main()
