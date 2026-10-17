"""Contract tests for the Gemini CLI Mezmo Governance hook."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mezmo_governance_hook as hook


AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
ENV = {
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


def hook_event(name: str) -> dict[str, Any]:
    event: dict[str, Any] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "transcript_path": "/tmp/demo/transcript.json",
        "cwd": "/tmp/demo",
        "timestamp": "2026-10-15T12:00:00Z",
        "tool_name": "mcp_kubernetes_demo_pods_delete",
        "tool_input": {"namespace": "staging", "name": "demo-pod"},
        "mcp_context": {
            "server_name": "kubernetes_demo",
            "tool_name": "pods_delete",
            "command": "kubernetes-mcp",
        },
    }
    if name == "AfterTool":
        event["tool_response"] = {
            "llmContent": "pod deleted",
            "returnDisplay": "pod deleted",
        }
    return event


def shell_event(name: str) -> dict[str, Any]:
    event = hook_event(name)
    event.pop("mcp_context")
    event["tool_name"] = "run_shell_command"
    event["tool_input"] = {"command": "kubectl delete pod demo-pod -n staging"}
    return event


class FakeHttp:
    def __init__(
        self,
        responses: list[tuple[int, dict[str, Any]] | BaseException],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None, float]] = []

    def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

class SlowHttp(FakeHttp):
    """Advances the fake clock during each call, like a slow answer."""

    def __init__(
        self,
        clock: FakeClock,
        seconds: float,
        responses: list[tuple[int, dict[str, Any]] | BaseException],
    ) -> None:
        super().__init__(responses)
        self.clock = clock
        self.seconds = seconds

    def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.clock.now += self.seconds
        return super().__call__(method, url, payload, timeout)



class GeminiCliGovernanceHookTests(unittest.TestCase):
    def test_uses_mcp_context_for_the_namespace(self) -> None:
        event = hook_event("BeforeTool")
        normalized = hook.normalize_tool_call(event)

        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.name, "pods_delete")
        self.assertEqual(normalized.arguments, event["tool_input"])

    def test_an_mcp_server_named_like_the_agent_is_refused(self) -> None:
        event = hook_event("BeforeTool")
        event["mcp_context"]["server_name"] = hook.AGENT
        with self.assertRaises(hook.HookContractError):
            hook.normalize_tool_call(event)

    def test_built_in_tools_use_the_gemini_cli_namespace(self) -> None:
        normalized = hook.normalize_tool_call(shell_event("BeforeTool"))

        self.assertEqual(normalized.namespace, "gemini_cli")
        self.assertEqual(normalized.name, "run_shell_command")

    def test_mcp_name_without_context_keeps_the_full_name(self) -> None:
        event = hook_event("BeforeTool")
        event.pop("mcp_context")
        normalized = hook.normalize_tool_call(event)

        self.assertEqual(normalized.namespace, "gemini_cli")
        self.assertEqual(normalized.name, "mcp_kubernetes_demo_pods_delete")

    def test_rejects_non_object_tool_input(self) -> None:
        event = hook_event("BeforeTool")
        event["tool_input"] = "ls"
        with self.assertRaises(hook.HookContractError):
            hook.normalize_tool_call(event)

    def test_a_repeated_identical_call_opens_a_new_decision(self) -> None:
        first = hook.approval_payload(shell_event("BeforeTool"))
        second = hook.approval_payload(shell_event("BeforeTool"))

        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertTrue(first["request_id"].startswith("gemini_cli:session-1:run-"))

    def test_approval_payload_identifies_gemini_cli(self) -> None:
        payload = hook.approval_payload(hook_event("BeforeTool"))

        self.assertEqual(
            payload["origin"],
            {"kind": "gemini_cli_hook", "reason": "BeforeTool"},
        )
        self.assertEqual(payload["scope"]["session_id"], "session-1")
        self.assertEqual(payload["items"][0]["tool_namespace"], "kubernetes_demo")

    @patch.dict(os.environ, ENV)
    def test_allows_an_approved_call_without_output(self) -> None:
        http = FakeHttp([(200, {"approved": True})])

        self.assertIsNone(hook.handle_event(hook_event("BeforeTool"), http=http))
        self.assertEqual(http.calls[0][0], "POST")

    @patch.dict(os.environ, ENV)
    def test_maps_a_denial_to_the_gemini_decision_fields(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])

        output = hook.handle_event(hook_event("BeforeTool"), http=http)

        self.assertEqual(output["decision"], "deny")
        self.assertIn("Use a canary first.", output["reason"])

    @patch.dict(os.environ, ENV)
    def test_polls_after_a_client_timeout_then_allows(self) -> None:
        http = FakeHttp(
            [
                TimeoutError("POST held too long"),
                (200, {"status": "approved", "reason": "ok"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("BeforeTool"), http=http, clock=clock, sleep=clock.sleep
        )

        self.assertIsNone(output)
        self.assertEqual(http.calls[1][0], "GET")

    @patch.dict(os.environ, ENV)
    def test_fails_closed_when_the_governance_request_fails(self) -> None:
        http = FakeHttp([hook.HookContractError("connection refused")])

        output = hook.handle_event(hook_event("BeforeTool"), http=http)

        self.assertEqual(output["decision"], "deny")
        self.assertIn("connection refused", output["reason"])

    def test_malformed_stdin_uses_the_blocking_exit_code(self) -> None:
        with patch.object(sys, "stdout", StringIO()), patch.object(sys, "stdin", StringIO("not-json")), patch.object(
            sys, "stderr", StringIO()
        ):
            self.assertEqual(hook.main(), 2)

    def audit(self, event: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}
            with patch.dict(os.environ, env):
                self.assertIsNone(hook.handle_event(event))
            lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def test_after_tool_writes_an_unjoined_audit_record(self) -> None:
        record = self.audit(hook_event("AfterTool"))

        self.assertEqual(record["agent"], "gemini_cli")
        self.assertEqual(record["tool_namespace"], "kubernetes_demo")
        self.assertEqual(record["status"], "observed")
        self.assertIsNone(record["decision_id"])
        self.assertIsNone(record["request_id"])

    def test_after_tool_with_an_error_is_failed(self) -> None:
        event = hook_event("AfterTool")
        event["tool_response"]["error"] = "permission denied"

        self.assertEqual(self.audit(event)["status"], "failed")

    @patch.dict(os.environ, ENV)
    def test_audit_failure_does_not_block(self) -> None:
        with patch.object(
            hook, "append_audit", side_effect=OSError("disk full")
        ), patch.object(sys, "stderr", StringIO()) as stderr:
            output = hook.handle_event(hook_event("AfterTool"))

        self.assertIsNone(output)
        self.assertIn("disk full", stderr.getvalue())


class PollingTests(unittest.TestCase):
    @patch.dict(os.environ, ENV)
    def test_a_call_sent_for_approval_polls_until_mezmo_decides(self) -> None:
        # Live Mezmo, 2026-10-16: a call sent for approval answers 207 with
        # approved false, polls 207 while pending, then 200 once decided.
        pending = {"approved": False, "reason": None}
        sent = {"approved": False, "reason": "No rule in this policy covers this request."}
        for approved in (True, False):
            with self.subTest(approved=approved):
                http = FakeHttp([(207, sent), (207, pending), (200, {"approved": approved, "reason": None})])
                clock = FakeClock()

                decision = hook.authorize(
                    {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
                )

                self.assertIs(decision.approved, approved)
                self.assertEqual([call[0] for call in http.calls], ["POST", "GET", "GET"])

    @patch.dict(os.environ, ENV)
    def test_poll_treats_404_as_pending_until_decided(self) -> None:
        http = FakeHttp(
            [
                (409, {"code": "ECONFLICT"}),
                (404, {}),
                (200, {"status": "approved", "reason": None}),
            ]
        )
        clock = FakeClock()

        decision = hook.authorize(
            {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
        )

        self.assertTrue(decision.approved)
        self.assertEqual([call[0] for call in http.calls], ["POST", "GET", "GET"])

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_poll_denies_when_404_lasts_past_the_deadline(self) -> None:
        http = FakeHttp([(409, {})] + [(404, {})] * 5)
        clock = FakeClock()

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_an_approval_posted_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(clock, 5.0, [(200, {"approved": True})])

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_an_approval_polled_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(
            clock, 1.5, [(409, {}), (200, {"status": "approved", "reason": None})]
        )

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )


class DeadlineWatchdogTests(unittest.TestCase):
    def fire(self, stdout: Any) -> list[int]:
        exits: list[int] = []
        fired = threading.Event()

        def fake_exit(code: int) -> None:
            exits.append(code)
            fired.set()

        with patch.object(sys, "stdout", stdout), patch.object(
            sys, "stderr", StringIO()
        ), patch.object(hook.os, "_exit", fake_exit):
            hook.deadline_watchdog(0.01)
            self.assertTrue(fired.wait(5))
        return exits

    def test_prints_the_deny_output_and_exits_with_the_block_code(self) -> None:
        stdout = StringIO()

        self.assertEqual(self.fire(stdout), [2])
        output = json.loads(stdout.getvalue())
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("approval still pending", stdout.getvalue())

    def test_exits_with_the_block_code_when_stdout_is_gone(self) -> None:
        class ClosedStdout(StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError("stdout closed")

        self.assertEqual(self.fire(ClosedStdout()), [2])

    def test_stopping_before_the_deadline_prints_nothing(self) -> None:
        with patch.object(hook.os, "_exit") as fake_exit, patch.object(
            sys, "stdout", StringIO()
        ) as stdout:
            hook.deadline_watchdog(0.05)()
            time.sleep(0.2)

        fake_exit.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")


class DeadlineBoundsTests(unittest.TestCase):
    def seconds(self, name: str, configured: str) -> float:
        with patch.dict(os.environ, {**ENV, name: configured}):
            if name == "MEZMO_GOVERNANCE_TIMEOUT_SECONDS":
                return hook.timeout_seconds()
            return hook.poll_interval_seconds()

    def test_timeout_at_or_above_the_hook_budget_is_capped(self) -> None:
        for configured in ("590", "600", "3600", "1e308"):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", configured),
                    hook.MAX_TIMEOUT_SECONDS,
                )
        self.assertLess(hook.MAX_TIMEOUT_SECONDS, 600)

    def test_small_positive_timeouts_are_kept(self) -> None:
        for configured, expected in (("30", 30.0), ("5e-324", 5e-324)):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", configured),
                    expected,
                )

    def test_rejects_seconds_that_are_not_finite_and_positive(self) -> None:
        names = (
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS",
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS",
        )
        for name in names:
            for configured in ("0", "-0", "-1", "nan", "inf", "-inf", "", "ten"):
                with self.subTest(name=name, configured=configured):
                    with self.assertRaises(hook.HookContractError):
                        self.seconds(name, configured)

    def test_poll_interval_has_a_floor(self) -> None:
        for configured in ("5e-324", "0.001"):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", configured),
                    hook.MIN_POLL_INTERVAL_SECONDS,
                )

    def test_a_large_timeout_still_ends_before_the_runtime_kills_the_hook(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        env = {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "3600"}
        with patch.dict(os.environ, env):
            hook.authorize({"decision_id": "d"}, http=http)
        self.assertLessEqual(http.calls[0][3], hook.MAX_TIMEOUT_SECONDS)

ADAPTER_DIR = Path(__file__).resolve().parent
# Hook configuration fields the Gemini CLI hooks reference lists.
GEMINI_HANDLER_KEYS = {"type", "command", "name", "timeout", "description"}

# Codex and Gemini CLI run the tool when a hook exits 1, for example on a crash.
CRASH_GUARD = " || { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }"


class HookConfigTests(unittest.TestCase):
    def test_example_config_registers_both_events(self) -> None:
        with (ADAPTER_DIR / "settings.example.json").open(encoding="utf-8") as handle:
            config = json.load(handle)["hooks"]

        self.assertEqual(set(config), {"BeforeTool", "AfterTool"})
        for event, groups in config.items():
            for group in groups:
                self.assertEqual(group["matcher"], "*")
                for handler in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertLessEqual(set(handler), GEMINI_HANDLER_KEYS)
                        self.assertEqual(handler["type"], "command")
                        self.assertIn("mezmo_governance_hook.py", handler["command"])
                        if event == "BeforeTool":
                            # Gemini CLI timeouts are in milliseconds.
                            self.assertGreater(
                                handler["timeout"],
                                hook.MAX_TIMEOUT_SECONDS * 1000,
                            )

    def test_extension_hooks_match_the_example_config(self) -> None:
        # `gemini extensions install` loads hooks/hooks.json and fills in ${extensionPath}.
        example = (ADAPTER_DIR / "settings.example.json").read_text(encoding="utf-8")
        example = example.replace(
            "${MEZMO_GOVERNANCE_HOOK:-$HOME/.gemini/hooks/mezmo_governance_hook.py}",
            "${extensionPath}${/}mezmo_governance_hook.py",
        )
        extension = json.loads((ADAPTER_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(extension, json.loads(example))
        manifest = json.loads((ADAPTER_DIR / "gemini-extension.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "mezmo-governance")

    def pre_and_post_commands(self) -> tuple[str, str]:
        with (ADAPTER_DIR / "settings.example.json").open(encoding="utf-8") as handle:
            config = json.load(handle)["hooks"]
        return (
            config["BeforeTool"][0]["hooks"][0]["command"],
            config["AfterTool"][0]["hooks"][0]["command"],
        )

    def run_command(self, command: str, hook_path: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MEZMO_GOVERNANCE_")
        }
        env["MEZMO_GOVERNANCE_HOOK"] = str(hook_path)
        return subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(hook_event("BeforeTool")) if stdin is None else stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_only_the_gating_command_carries_the_crash_guard(self) -> None:
        pre, post = self.pre_and_post_commands()

        self.assertTrue(pre.endswith(CRASH_GUARD))
        self.assertNotIn("exit 2", post)

    def test_the_crash_guard_turns_a_crash_into_a_block(self) -> None:
        pre, _ = self.pre_and_post_commands()
        with tempfile.TemporaryDirectory() as tmp:
            crashing = Path(tmp) / "crash.py"
            crashing.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
            result = self.run_command(pre, crashing)

        self.assertEqual(result.returncode, 2)
        self.assertIn("Mezmo Governance hook failed; blocking", result.stderr)

    def test_a_deliberate_block_prints_no_crash_message(self) -> None:
        # The deadline watchdog writes its reason and exits 2; so does the hook on input it cannot read.
        pre, _ = self.pre_and_post_commands()
        with tempfile.TemporaryDirectory() as tmp:
            watchdog = Path(tmp) / "watchdog.py"
            watchdog.write_text(
                "import os, sys\nsys.stderr.write('Mezmo Governance: approval still pending\\n')\nos._exit(2)\n",
                encoding="utf-8",
            )
            for path, stdin in ((watchdog, None), (HOOK_PATH, "not json")):
                with self.subTest(path=path.name):
                    result = self.run_command(pre, path, stdin)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("hook failed", result.stderr)

    def test_a_hook_that_does_not_run_still_blocks(self) -> None:
        pre, _ = self.pre_and_post_commands()
        # Python names a missing script itself, and exits 2.
        result = self.run_command(pre, Path("/nonexistent/mezmo_governance_hook.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("/nonexistent/mezmo_governance_hook.py", result.stderr)
        result = self.run_command(pre.replace("python3", "python3-not-on-path", 1), HOOK_PATH)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Mezmo Governance hook failed; blocking", result.stderr)

    def test_the_crash_guard_keeps_the_hook_deny_output(self) -> None:
        pre, _ = self.pre_and_post_commands()
        result = self.run_command(pre, HOOK_PATH)

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("MEZMO_GOVERNANCE_URL", result.stdout)


HOOK_PATH = Path(__file__).resolve().with_name("mezmo_governance_hook.py")
PYTHON_FLOOR = (3, 9)


def runtime_union_lines(source: str) -> list[int]:
    """Lines that evaluate `X | Y` at runtime, which Python 3.9 rejects for types."""
    tree = ast.parse(source)
    annotations: set[int] = set()
    for node in ast.walk(tree):
        roots: list[ast.AST | None] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ]
            roots.extend(argument.annotation for argument in every if argument)
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        for root in roots:
            if root is not None:
                annotations.update(id(child) for child in ast.walk(root))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.BitOr)
        and id(node) not in annotations
    ]


def floor_interpreter() -> str | None:
    for candidate in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(candidate)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() == "True":
            return path
    return None


class PythonFloorTests(unittest.TestCase):
    def test_hook_has_no_runtime_union_syntax(self) -> None:
        source = HOOK_PATH.read_text(encoding="utf-8")
        self.assertEqual(runtime_union_lines(source), [])

    def test_hook_imports_on_the_oldest_supported_python(self) -> None:
        interpreter = floor_interpreter()
        if interpreter is None:
            self.skipTest("no Python 3.9 interpreter on PATH")
        result = subprocess.run(
            [
                interpreter,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import mezmo_governance_hook",
                str(HOOK_PATH.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


PRE_EVENT = "BeforeTool"
POST_EVENT = "AfterTool"
ALLOW_OUTPUT = None
ORIGIN_KIND = "gemini_cli_hook"


def deny_schema(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


class StubGovernance(ThreadingHTTPServer):
    """Local Mezmo Governance stand-in that replays scripted answers."""

    def __init__(self, answers: list[tuple[int, dict[str, Any]]]) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> StubGovernance:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
        self.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/governance/workflow/authorize"


class StubHandler(BaseHTTPRequestHandler):
    server: StubGovernance

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw) if raw else None,
            }
        )
        status, body = self.server.answers.pop(0)
        # Bytes are sent as they are, to stand in for a broken reply.
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        if isinstance(body, dict) and "Location" in body:
            self.send_header("Location", body["Location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = answer
    do_POST = answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


class DripHandler(BaseHTTPRequestHandler):
    """Sends an approval one byte at a time, each byte inside the socket timeout."""

    server: DripGovernance

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"approved":true' + b" " * 40 + b"}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for byte in body:
            if self.server.stopped.wait(0.3):
                return
            try:
                self.wfile.write(bytes([byte]))
            except OSError:
                return

    def log_message(self, format: str, *args: Any) -> None:
        pass


class DripGovernance(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), DripHandler)
        self.stopped = threading.Event()

    def __enter__(self) -> DripGovernance:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stopped.set()
        self.shutdown()
        self.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/governance/workflow/authorize"


class EndToEndTests(unittest.TestCase):
    """Run the script as the runtime does: JSON on stdin, decision on stdout."""

    def run_hook(
        self,
        event: dict[str, Any] | str,
        url: str | None,
        extra: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MEZMO_GOVERNANCE_")
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(
            {
                "NO_PROXY": "*",
                "MEZMO_GOVERNANCE_TOKEN": "sts_test",
                "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "20",
                "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
                "MEZMO_GOVERNANCE_AUDIT_LOG": str(Path(self.tmp.name) / "audit.jsonl"),
            }
        )
        if url is not None:
            env["MEZMO_GOVERNANCE_URL"] = url
        env.update(extra or {})
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=event if isinstance(event, str) else json.dumps(event),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_approved_call_runs(self) -> None:
        with StubGovernance([(200, {"approved": True})]) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stdout_json(result), ALLOW_OUTPUT)
        self.assertEqual(stub.requests[0]["method"], "POST")
        self.assertEqual(stub.requests[0]["authorization"], "Token sts_test")
        self.assertEqual(stub.requests[0]["body"]["origin"]["kind"], ORIGIN_KIND)

    def test_denied_call_prints_the_runtime_deny_schema(self) -> None:
        with StubGovernance([(200, {"approved": False, "reason": "No."})]) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stdout_json(result),
            deny_schema("Mezmo Governance denied this exact tool call: No."),
        )

    def test_a_redirect_denies_and_never_sends_the_key_elsewhere(self) -> None:
        cases = [
            ([], (200, {"approved": True})),
            ([(409, {})], (200, {"status": "approved"})),
        ]
        for before, approval in cases:
            with self.subTest(after_conflict=bool(before)):
                with StubGovernance([approval]) as elsewhere:
                    redirect = (302, {"Location": elsewhere.url})
                    with StubGovernance([*before, redirect]) as stub:
                        result = self.run_hook(hook_event(PRE_EVENT), stub.url)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                self.assertIn("HTTP 302", json.dumps(stdout_json(result)))
                self.assertEqual(elsewhere.requests, [])

    def test_conflict_polls_the_existing_decision(self) -> None:
        answers = [(409, {"code": "ECONFLICT"}), (200, {"status": "denied", "reason": "No."})]
        with StubGovernance(answers) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stdout_json(result),
            deny_schema("Mezmo Governance denied this exact tool call: No."),
        )
        decision_id = stub.requests[0]["body"]["decision_id"]
        self.assertEqual(stub.requests[1]["method"], "GET")
        self.assertIn(f"decision_id={decision_id}", stub.requests[1]["path"])

    def test_missing_url_denies(self) -> None:
        result = self.run_hook(hook_event(PRE_EVENT), None)

        self.assertEqual(result.returncode, 0, result.stderr)
        output = stdout_json(result)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("MEZMO_GOVERNANCE_URL", json.dumps(output))

    def test_a_reply_it_cannot_read_denies(self) -> None:
        cases = [
            ([(200, b"<html>bad gateway</html>")], "invalid JSON response"),
            ([(200, b"[]")], "non-object response"),
            ([(200, b"")], "did not return an approval decision"),
            ([(200, {"reason": "no field"})], "did not return an approval decision"),
            ([(200, {"approved": "true"})], "did not return an approval decision"),
            ([(200, {"approved": 1})], "did not return an approval decision"),
            ([(409, {}), (200, {"status": "maybe"})], "unexpected status"),
            ([(409, {}), (200, {})], "unexpected status"),
            ([(409, {}), (200, {"approved": "yes"})], "unexpected status"),
            ([(409, {}), (200, b"not json")], "invalid JSON response"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                with StubGovernance(answers) as stub:
                    result = self.run_hook(hook_event(PRE_EVENT), stub.url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                self.assertIn(message, json.dumps(stdout_json(result)))

    def test_a_mezmo_error_shows_its_code_and_message(self) -> None:
        enoagent = {
            "status": 403,
            "code": "ENOAGENT",
            "message": "no agent is registered for these credentials",
        }
        noisy = {"code": "EINVAL", "message": "bad\nline\x1b[2J " + "x" * 500, "errors": ["secret-arg"]}
        cases = [
            (
                [(403, enoagent)],
                "Mezmo Governance returned HTTP 403 ENOAGENT: "
                "no agent is registered for these credentials",
            ),
            (
                [(409, {}), (401, {"code": "EAUTH", "message": "Unauthorized"})],
                "Mezmo Governance returned HTTP 401 EAUTH while polling: Unauthorized",
            ),
            ([(400, noisy)], "Mezmo Governance returned HTTP 400 EINVAL: bad line [2J xxx"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                with StubGovernance(answers) as stub:
                    result = self.run_hook(hook_event(PRE_EVENT), stub.url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                shown = json.dumps(stdout_json(result))
                self.assertIn(message, shown)
                # One line, bounded, and nothing beyond code and message.
                self.assertNotIn("\\n", shown)
                self.assertNotIn("\\u001b", shown)
                self.assertNotIn("x" * 201, shown)
                self.assertNotIn("secret-arg", shown)

    def test_a_url_that_would_expose_the_token_is_refused_before_any_request(self) -> None:
        urls = [
            "http://192.0.2.1/governance/workflow/authorize",
            "http://api.example.test/governance/workflow/authorize",
            "ftp://192.0.2.1/governance",
            "file:///etc/hosts",
            "https://user:pw_secret@api.example.test/governance/workflow/authorize",
        ]
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TOKEN": "sts_test"}), patch.object(
            hook, "build_opener"
        ) as opener:
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(hook.HookContractError) as caught:
                        hook.http_json("POST", url, {}, 1.0)
                    self.assertNotIn("pw_secret", str(caught.exception))
        opener.assert_not_called()

    def test_http_to_this_machine_still_works(self) -> None:
        hosts = ["127.0.0.1", "127.0.0.2", "localhost", "[::1]"]
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TOKEN": "sts_test"}), patch.object(
            hook, "build_opener"
        ) as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"approved": true}'
            for host in hosts:
                with self.subTest(host=host):
                    url = f"http://{host}:8787/governance/workflow/authorize"
                    self.assertEqual(
                        hook.http_json("POST", url, {}, 1.0), (200, {"approved": True})
                    )

    def test_no_deny_reason_shows_the_token(self) -> None:
        with StubGovernance([]) as stub:
            cases = [
                (stub.url, {"MEZMO_GOVERNANCE_TOKEN": "sts_secret\nX-Injected: 1"}),
                (stub.url.replace("http://", "http://user:sts_secret@"), {}),
            ]
            for url, extra in cases:
                with self.subTest(url=url, extra=extra):
                    result = self.run_hook(hook_event(PRE_EVENT), url, extra=extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
                    self.assertNotIn("sts_secret", result.stdout + result.stderr)
            self.assertEqual(stub.requests, [])

    def test_unreachable_governance_denies(self) -> None:
        with StubGovernance([]) as stub:
            url = stub.url
        result = self.run_hook(hook_event(PRE_EVENT), url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable", json.dumps(stdout_json(result)))

    def test_unsupported_event_exits_2(self) -> None:
        event = hook_event(PRE_EVENT)
        event["hook_event_name"] = "SomethingElse"
        result = self.run_hook(event, None)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
        self.assertIn("unsupported hook event", result.stderr)

    def test_input_too_deep_to_read_is_denied_with_the_block_code(self) -> None:
        # Python 3.9 to 3.13 stop at 1000 levels, later versions at the C stack.
        depth = 200_000
        raw = json.dumps(hook_event(PRE_EVENT))[:-1]
        raw += ', "nested": ' + "[" * depth + "]" * depth + "}"
        result = self.run_hook(raw, None)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX file modes")
    def test_the_audit_log_is_private_to_its_owner(self) -> None:
        # The log keeps up to 2000 characters of each tool result.
        fresh = Path(self.tmp.name) / "fresh" / "audit.jsonl"
        older = Path(self.tmp.name) / "older.jsonl"
        older.write_text("", encoding="utf-8")
        older.chmod(0o644)
        for path in (fresh, older):
            with self.subTest(log=path.name), StubGovernance([]) as stub:
                result = self.run_hook(
                    hook_event(POST_EVENT),
                    stub.url,
                    extra={"MEZMO_GOVERNANCE_AUDIT_LOG": str(path)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(fresh.parent.stat().st_mode), 0o700)

    def test_post_event_logs_without_calling_governance(self) -> None:
        with StubGovernance([]) as stub:
            result = self.run_hook(hook_event(POST_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(stub.requests, [])
        log = Path(self.tmp.name) / "audit.jsonl"
        self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["agent"], hook.AGENT)

    def test_a_dripping_answer_is_denied_at_the_deadline(self) -> None:
        with DripGovernance() as stub:
            started = time.monotonic()
            result = self.run_hook(
                hook_event(PRE_EVENT),
                stub.url,
                extra={"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"},
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 10, "the hook waited past its deadline")
        self.assertEqual(result.returncode, 2, result.stderr)
        output = stdout_json(result)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("approval still pending", json.dumps(output))


def stdout_json(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
