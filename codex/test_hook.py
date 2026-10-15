"""Contract tests for the Codex Mezmo Governance hook."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
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
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


def hook_event(name: str) -> dict[str, Any]:
    event: dict[str, Any] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "tool-use-1",
        "tool_name": "mcp__kubernetes_demo__pods_delete",
        "tool_input": {"namespace": "staging", "name": "demo-pod"},
        "cwd": "/tmp/demo",
        "permission_mode": "default",
    }
    if name == "PostToolUse":
        event["tool_response"] = {"content": "pod deleted", "isError": False}
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


class CodexGovernanceHookTests(unittest.TestCase):
    def test_normalizes_mcp_identity_without_changing_arguments(self) -> None:
        event = hook_event("PreToolUse")
        normalized = hook.normalize_tool_call(event)

        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.name, "pods_delete")
        self.assertEqual(normalized.arguments, event["tool_input"])

    def test_reuses_the_same_identity_for_pre_and_post_events(self) -> None:
        before = hook.call_identity(hook_event("PreToolUse"))
        after = hook.call_identity(hook_event("PostToolUse"))

        self.assertEqual(before, after)
        self.assertEqual(
            before.request_id,
            "codex:session-1:turn-1:tool-use-1",
        )

    def test_approval_payload_identifies_codex_and_the_mcp_namespace(self) -> None:
        payload = hook.approval_payload(hook_event("PreToolUse"))

        self.assertEqual(
            payload["origin"],
            {"kind": "codex_hook", "reason": "PreToolUse"},
        )
        self.assertEqual(payload["items"][0]["tool_namespace"], "kubernetes_demo")
        self.assertEqual(payload["items"][0]["tool_name"], "pods_delete")

    @patch.dict(os.environ, ENV)
    def test_allows_an_approved_call_without_rewriting_it(self) -> None:
        http = FakeHttp([(200, {"approved": True})])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertIsNone(output)
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][1], AUTHORIZE_URL)
        self.assertEqual(
            http.calls[0][2]["items"][0]["arguments"],
            hook_event("PreToolUse")["tool_input"],
        )

    @patch.dict(os.environ, ENV)
    def test_maps_a_denial_to_a_valid_pre_tool_hook_decision(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn(
            "Use a canary first.",
            output["hookSpecificOutput"]["permissionDecisionReason"],
        )

    @patch.dict(os.environ, ENV)
    def test_polls_after_a_client_timeout_then_allows(self) -> None:
        http = FakeHttp(
            [
                TimeoutError("POST held too long"),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "ok"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("PreToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertIsNone(output)
        self.assertEqual(http.calls[1][0], "GET")
        self.assertIn("decision_id=", http.calls[1][1])

    @patch.dict(os.environ, ENV)
    def test_polls_after_conflict_then_denies(self) -> None:
        http = FakeHttp(
            [
                (409, {"code": "ECONFLICT"}),
                (200, {"status": "denied", "reason": "already decided"}),
            ]
        )

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("already decided", output["hookSpecificOutput"]["permissionDecisionReason"])

    @patch.dict(os.environ, ENV)
    def test_fails_closed_when_the_governance_request_fails(self) -> None:
        http = FakeHttp([hook.HookContractError("connection refused")])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn(
            "connection refused",
            output["hookSpecificOutput"]["permissionDecisionReason"],
        )

    @patch.dict(os.environ, ENV)
    def test_fails_closed_when_approval_stays_pending(self) -> None:
        http = FakeHttp(
            [
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "pending", "reason": None}),
            ]
        )
        clock = FakeClock()
        env = {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "1"}
        with patch.dict(os.environ, env):
            http = FakeHttp(
                [
                    TimeoutError("held"),
                    (200, {"status": "pending", "reason": None}),
                    (200, {"status": "pending", "reason": None}),
                ]
            )
            output = hook.handle_event(
                hook_event("PreToolUse"),
                http=http,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("approval still pending", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_malformed_stdin_uses_the_blocking_exit_code(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("not-json")),
            patch.object(sys, "stderr", StringIO()),
        ):
            exit_code = hook.main()

        self.assertEqual(exit_code, 2)

    @patch.dict(os.environ, ENV)
    def test_reports_post_tool_output_locally_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}
            with patch.dict(os.environ, env):
                output = hook.handle_event(hook_event("PostToolUse"))

            self.assertIsNone(output)
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["agent"], "codex")
            self.assertEqual(records[0]["tool_name"], "pods_delete")
            self.assertEqual(records[0]["status"], "observed")

    @patch.dict(os.environ, ENV)
    def test_post_tool_audit_failure_does_not_block(self) -> None:
        with (
            patch.object(hook, "append_audit", side_effect=OSError("disk full")),
            patch.object(sys, "stderr", StringIO()) as stderr,
        ):
            output = hook.handle_event(hook_event("PostToolUse"))

        self.assertIsNone(output)
        self.assertIn("disk full", stderr.getvalue())

    def test_authorization_header_prefixes_token(self) -> None:
        with patch.dict(os.environ, ENV):
            self.assertEqual(hook.authorization_header(), "Token sts_test")
        with patch.dict(
            os.environ,
            {**ENV, "MEZMO_GOVERNANCE_ACCESS_KEY": "Token sts_test"},
        ):
            self.assertEqual(hook.authorization_header(), "Token sts_test")


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
# Handler keys the Codex hooks docs list for command hooks.
CODEX_HANDLER_KEYS = {
    "type",
    "command",
    "commandWindows",
    "timeout",
    "statusMessage",
    "async",
    "additionalContextLimit",
}


class HookConfigTests(unittest.TestCase):
    def test_example_config_registers_both_events(self) -> None:
        with (ADAPTER_DIR / "hooks.example.json").open(encoding="utf-8") as handle:
            config = json.load(handle)["hooks"]

        self.assertEqual(set(config), {"PreToolUse", "PostToolUse"})
        for event, groups in config.items():
            for group in groups:
                self.assertEqual(group["matcher"], ".*")
                for handler in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertLessEqual(set(handler), CODEX_HANDLER_KEYS)
                        self.assertEqual(handler["type"], "command")
                        self.assertIn("mezmo_governance_hook.py", handler["command"])
                        if event == "PreToolUse":
                            self.assertGreater(
                                handler["timeout"],
                                hook.MAX_TIMEOUT_SECONDS,
                            )


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


PRE_EVENT = "PreToolUse"
POST_EVENT = "PostToolUse"
ALLOW_OUTPUT = None
ORIGIN_KIND = "codex_hook"


def deny_schema(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


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
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = answer
    do_POST = answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


class EndToEndTests(unittest.TestCase):
    """Run the script as the runtime does: JSON on stdin, decision on stdout."""

    def run_hook(
        self,
        event: dict[str, Any],
        url: str | None,
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
                "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
                "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "20",
                "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
                "MEZMO_GOVERNANCE_AUDIT_LOG": str(Path(self.tmp.name) / "audit.jsonl"),
            }
        )
        if url is not None:
            env["MEZMO_GOVERNANCE_URL"] = url
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(event),
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
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported hook event", result.stderr)

    def test_post_event_logs_without_calling_governance(self) -> None:
        with StubGovernance([]) as stub:
            result = self.run_hook(hook_event(POST_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(stub.requests, [])
        log = Path(self.tmp.name) / "audit.jsonl"
        self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["agent"], hook.AGENT)


def stdout_json(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
