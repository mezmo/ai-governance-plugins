"""Tests for scripts/ledger_investigation.py, the CLI that joins, closes and launches investigations.

`id`, `join` and `close` run in this process with a cleared environment. `launch` execs its
command, so it runs as a script against an audit-service defined here, with the retry file and
session state in a temp folder and no setting of the developer's own.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any, Dict, List, Tuple
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ledger_investigation  # noqa: E402

ACCOUNT = "test-acct"
# The epic's check value: account test-acct and this incident key.
INCIDENT_KEY = "pagerduty:Q34FCCWSTU9OI2"
CHECK_THREAD_ID = "ab2763da-3eea-5b82-a1bf-7922641cdf6c"
# Prints the two variables launch sets, as the command sees them.
PRINT_ENV = (
    "import json, os; print(json.dumps({name: os.environ.get(name) for name in "
    "('MEZMO_LEDGER_INCIDENT_KEY', 'MEZMO_LEDGER_INVESTIGATION_ID')}))"
)
# Creates the file named by its argument, so a test can tell whether the command ran.
TOUCH = "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()"


class Recorder:
    """What the in-test audit-service answers, and what it was sent."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status = 202


def serve(test: unittest.TestCase, recorder: Recorder) -> str:
    """Start a server for `recorder` and return its events URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorder.requests.append({"body": json.loads(raw), "headers": dict(self.headers), "path": self.path})
            body = b'{"accepted":1}'
            self.send_response(recorder.status)
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


class PrintTests(unittest.TestCase):
    """`id`, `join` and `close`: one line of JSON, and no I/O beyond it."""

    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"MEZMO_LEDGER_ACCOUNT": ACCOUNT}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def run_main(self, *argv: str) -> Tuple[int, str, str]:
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            code = ledger_investigation.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_id_prints_the_pinned_thread_id(self) -> None:
        code, stdout, stderr = self.run_main("id", "--incident-key", INCIDENT_KEY)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, '{"incident_key":"pagerduty:Q34FCCWSTU9OI2","thread_id":"%s"}\n' % CHECK_THREAD_ID)

    def test_join_and_close_print_their_one_line(self) -> None:
        for command, action in (("close", "closed"), ("join", "joined")):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_main(command, "--incident-key", INCIDENT_KEY)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(stdout.count("\n"), 1)
                self.assertEqual(
                    json.loads(stdout), {"action": action, "incident_key": INCIDENT_KEY, "thread_id": CHECK_THREAD_ID}
                )

    def test_a_key_that_is_not_source_colon_id_is_refused(self) -> None:
        for key in ("PagerDuty:Q34FCCWSTU9OI2", "github:123", "jira", "jira:", "jira:AB 12", "slack:C1\tT2"):
            with self.subTest(key=key):
                code, stdout, stderr = self.run_main("id", "--incident-key", key)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("incident key must be <source>:<id>", stderr)
        with self.subTest(key="surrounding whitespace"):
            code, stdout, _ = self.run_main("id", "--incident-key", f"  {INCIDENT_KEY}\n")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout), {"incident_key": INCIDENT_KEY, "thread_id": CHECK_THREAD_ID})


class LaunchTests(unittest.TestCase):
    """`launch` run as a script, since it execs its command."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = pathlib.Path(scratch.name)
        self.marker = self.dir / "ran"
        self.spool = self.dir / "spool" / "ledger-spool.jsonl"
        self.recorder = Recorder()
        git_config = self.dir / "gitconfig"
        git_config.touch()
        # Built from nothing, so no setting of the developer's own reaches the script. Git reads
        # an empty config and no repository, so it reports no user email.
        self.env = {
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": str(self.dir / "no-git"),
            "HOME": str(self.dir),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.dir / "audit.jsonl"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "sessions"),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(self.spool),
            "MEZMO_LEDGER_URL": serve(self, self.recorder),
            "NO_PROXY": "*",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def launch(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "ledger_investigation.py"), *argv],
            capture_output=True,
            check=False,
            # A folder outside any repository, so git finds no repository config either.
            cwd=self.dir,
            env=self.env,
            text=True,
            timeout=30,
        )

    def touch(self) -> List[str]:
        return ["--", sys.executable, "-c", TOUCH, str(self.marker)]

    def events(self) -> List[Dict[str, Any]]:
        return [event for request in self.recorder.requests for event in request["body"]]

    def test_launch_sends_opened_and_sets_both_variables_for_the_command(self) -> None:
        result = self.launch("launch", "--incident-key", INCIDENT_KEY, "--", sys.executable, "-c", PRINT_ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"MEZMO_LEDGER_INCIDENT_KEY": INCIDENT_KEY, "MEZMO_LEDGER_INVESTIGATION_ID": CHECK_THREAD_ID},
        )
        (opened,) = self.events()
        self.assertEqual(opened["actor"], {"account": ACCOUNT, "pk": "claude_code:launcher", "type": "agent"})
        self.assertEqual(opened["application"]["name"], "mezmo-claude-code-plugin")
        self.assertEqual(
            (opened["correlation_id"], opened["entity_id"], opened["entity_type"], opened["event"]),
            (CHECK_THREAD_ID, CHECK_THREAD_ID, "investigation", "agent.investigation.opened"),
        )
        # Whole, with no session_id and, since git reports no email here, no operator_email.
        self.assertEqual(
            opened["extra"],
            {"agent": "claude_code", "incident_key": INCIDENT_KEY, "opened_by": "alert", "thread_id": CHECK_THREAD_ID},
        )
        self.assertFalse(self.spool.exists())

    def test_launch_exits_as_the_command_does_and_refuses_to_run_without_its_settings(self) -> None:
        with self.subTest(case="the command's exit code"):
            result = self.launch("launch", "--incident-key", INCIDENT_KEY, "--", sys.executable, "-c", "exit(7)")
            self.assertEqual(result.returncode, 7, result.stderr)
        self.recorder.requests.clear()
        missing = ["--", str(self.dir / "no-such-command")]
        for case, argv, env, code in (
            ("no command", ["launch", "--incident-key", INCIDENT_KEY], {}, 2),
            ("an empty command", ["launch", "--incident-key", INCIDENT_KEY, "--"], {}, 2),
            ("no key argument", ["launch", *self.touch()], {}, 2),
            ("no account", ["launch", "--incident-key", INCIDENT_KEY, *self.touch()], {"MEZMO_LEDGER_ACCOUNT": ""}, 2),
            ("no URL", ["launch", "--incident-key", INCIDENT_KEY, *self.touch()], {"MEZMO_LEDGER_URL": ""}, 2),
            ("a command that does not exist", ["launch", "--incident-key", INCIDENT_KEY, *missing], {}, 127),
            ("an abbreviated key argument", ["join", "--incident", INCIDENT_KEY], {}, 2),
        ):
            with self.subTest(case=case), patch.dict(self.env, env):
                result = self.launch(*argv)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotEqual(result.stderr, "")
                self.assertFalse(self.marker.exists())
                self.assertEqual(self.recorder.requests, [])
                self.assertFalse(self.spool.exists())
        with self.subTest(case="a failed send"):
            self.recorder.status = 503
            result = self.launch("launch", "--incident-key", INCIDENT_KEY, *self.touch())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(self.marker.exists())
            self.assertIn("saving it for retry", result.stderr)
            (line,) = self.spool.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(line)["event"], self.events()[0])
            self.assertEqual(json.loads(line)["event"]["event"], "agent.investigation.opened")


if __name__ == "__main__":
    unittest.main()
