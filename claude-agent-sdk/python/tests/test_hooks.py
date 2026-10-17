"""Tests for the Claude Agent SDK hook callbacks.

The callbacks get the input the SDK passes them: the hook input object, the
tool use id and a context. A local HTTP server stands in for Mezmo.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mezmo_governance_claude_agent_sdk.client import call_digest  # noqa: E402
from mezmo_governance_claude_agent_sdk.hooks import (  # noqa: E402
    PRE_TOOL_USE_TIMEOUT_SECONDS,
    create_pre_tool_use,
    mezmo_governance_hooks,
    post_tool_use,
)

# The identity of the fixture call, mcp__notes__delete_note on {"name": "todo"}.
NOTE_DIGEST = call_digest("notes", "delete_note", {"name": "todo"})

try:
    import httpx  # noqa: F401
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    from claude_agent_sdk import HookMatcher
except ImportError:
    HookMatcher = None  # type: ignore[misc, assignment]

ENV = {
    "MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize",
    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


def pre_event(**overrides: Any) -> dict[str, Any]:
    """A PreToolUse input as the SDK sends it for a custom SDK MCP tool."""
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "permission_mode": "bypassPermissions",
        "tool_name": "mcp__notes__delete_note",
        "tool_input": {"name": "todo"},
        "tool_use_id": "toolu_01",
    }
    event.update(overrides)
    return event


class FakeHttp:
    def __init__(self, responses: list[tuple[int, dict[str, Any]] | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None, float]] = []

    async def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, payload, timeout))
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def reason(output: dict[str, Any]) -> str:
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


class PreToolUseTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, ENV)
    async def test_approved_call_returns_empty_output(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertEqual(output, {})
        method, url, payload, _ = http.calls[0]
        self.assertEqual((method, url), ("POST", ENV["MEZMO_GOVERNANCE_URL"]))
        assert payload is not None
        self.assertEqual(payload["request_id"], "claude_agent_sdk:sess-1:toolu_01#" + NOTE_DIGEST)
        self.assertEqual(payload["scope"], {"kind": "single", "session_id": "sess-1"})
        self.assertEqual(payload["origin"], {"kind": "claude_agent_sdk_hooks", "reason": "PreToolUse"})
        self.assertEqual(
            payload["items"],
            [{"id": 0, "tool_namespace": "notes", "tool_name": "delete_note", "arguments": {"name": "todo"}}],
        )

    @patch.dict(os.environ, ENV)
    async def test_built_in_tools_use_the_agent_namespace(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        event = pre_event(tool_name="Bash", tool_input={"command": "ls"})
        await create_pre_tool_use(http=http)(event, "toolu_01", {"signal": None})
        item = http.calls[0][2]["items"][0]  # type: ignore[index]
        self.assertEqual((item["tool_namespace"], item["tool_name"]), ("claude_agent_sdk", "Bash"))

    @patch.dict(os.environ, ENV)
    async def test_denied_call_carries_the_reason(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "deletes need a person"})])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertEqual(
            reason(output),
            "Mezmo Governance denied this exact tool call: deletes need a person",
        )

    @patch.dict(os.environ, ENV)
    async def test_deny_without_a_reason_still_says_why(self) -> None:
        http = FakeHttp([(200, {"approved": False})])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertIn("Policy denied the call.", reason(output))

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "1"})
    async def test_a_decision_still_pending_at_the_deadline_is_denied(self) -> None:
        http = FakeHttp([(409, {}), (200, {"status": "pending", "reason": None})])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertIn("approval still pending", reason(output))
        self.assertEqual([call[0] for call in http.calls[:2]], ["POST", "GET"])

    @patch.dict(os.environ, ENV)
    async def test_a_held_post_falls_into_polling(self) -> None:
        http = FakeHttp([TimeoutError("held"), (200, {"status": "approved", "reason": "ok"})])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertEqual(output, {})
        self.assertIn("decision_id=", http.calls[1][1])

    @patch.dict(os.environ, ENV)
    async def test_a_call_without_a_tool_use_id_gets_a_random_one(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        hook = create_pre_tool_use(http=http)
        event = pre_event()
        del event["tool_use_id"]
        await hook(event, None, {"signal": None})
        await hook(event, None, {"signal": None})
        first, second = (call[2] for call in http.calls)
        assert first is not None and second is not None
        self.assertTrue(first["request_id"].startswith("claude_agent_sdk:sess-1:generated-"))
        self.assertNotEqual(first["decision_id"], second["decision_id"])

    @patch.dict(os.environ, ENV)
    async def test_the_tool_use_id_argument_wins_over_the_input_field(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        await create_pre_tool_use(http=http)(pre_event(tool_use_id="other"), "toolu_02", {"signal": None})
        self.assertEqual(http.calls[0][2]["request_id"], "claude_agent_sdk:sess-1:toolu_02#" + NOTE_DIGEST)  # type: ignore[index]

    @patch.dict(os.environ, ENV)
    async def test_malformed_input_is_denied_without_asking_mezmo(self) -> None:
        cases = [
            ("not an object", "hook input must be a JSON object"),
            (pre_event(tool_input="rm -rf /"), "tool_input must be a JSON object"),
            (pre_event(session_id=""), "session_id must be a non-empty string"),
            (pre_event(tool_name=None), "tool_name must be a non-empty string"),
            (pre_event(tool_name="mcp__broken"), "mcp__<server>__<tool>"),
        ]
        for event, message in cases:
            with self.subTest(message=message):
                http = FakeHttp([(200, {"approved": True})])
                output = await create_pre_tool_use(http=http)(event, "toolu_01", {"signal": None})
                self.assertIn(message, reason(output))
                self.assertEqual(http.calls, [])

    async def test_missing_settings_deny(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            output = await create_pre_tool_use()(pre_event(), "toolu_01", {"signal": None})
        self.assertIn("MEZMO_GOVERNANCE_URL must be a non-empty string", reason(output))

    @patch.dict(os.environ, ENV)
    async def test_a_transport_that_raises_is_denied(self) -> None:
        http = FakeHttp([RuntimeError("boom")])
        output = await create_pre_tool_use(http=http)(pre_event(), "toolu_01", {"signal": None})
        self.assertIn("could not authorize this exact tool call", reason(output))
        self.assertIn("boom", reason(output))


class RawHandler(BaseHTTPRequestHandler):
    """Replays the server's queued (status, body bytes) answers and records requests."""

    def answer(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.command, self.headers.get("Authorization"), body))  # type: ignore[attr-defined]
        status, reply = self.server.answers.pop(0)  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = answer
    do_POST = answer

    def log_message(self, *args: object) -> None:
        pass


def serve(answers: list[tuple[int, bytes]]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), RawHandler)
    server.answers = list(answers)  # type: ignore[attr-defined]
    server.seen = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


def url_of(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/governance/workflow/authorize"


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(httpx is not None, "httpx is not installed")
class RealTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_over_http_sends_the_key(self) -> None:
        server = serve([(200, b'{"approved": true}')])
        try:
            with patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_URL": url_of(server)}):
                output = await create_pre_tool_use()(pre_event(), "toolu_01", {"signal": None})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(output, {})
        method, authorization, body = server.seen[0]  # type: ignore[attr-defined]
        self.assertEqual((method, authorization), ("POST", "Token sts_test"))
        self.assertEqual(json.loads(body)["origin"]["kind"], "claude_agent_sdk_hooks")

    async def test_endpoint_and_key_arguments_win_over_the_variables(self) -> None:
        server = serve([(200, b'{"approved": false, "reason": "nope"}')])
        try:
            with patch.dict(os.environ, ENV):
                hook = create_pre_tool_use(url_of(server), access_key="Bearer other")
                output = await hook(pre_event(), "toolu_01", {"signal": None})
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("nope", reason(output))
        self.assertEqual(server.seen[0][1], "Bearer other")  # type: ignore[attr-defined]

    async def test_unreachable_mezmo_denies(self) -> None:
        url = f"http://127.0.0.1:{closed_port()}/governance/workflow/authorize"
        with patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_URL": url}):
            output = await create_pre_tool_use()(pre_event(), "toolu_01", {"signal": None})
        self.assertIn("could not authorize this exact tool call", reason(output))
        self.assertIn("Mezmo Governance is unavailable", reason(output))

    async def test_a_reply_it_cannot_read_denies(self) -> None:
        cases = [
            ([(200, b"<html>bad gateway</html>")], "invalid JSON response"),
            ([(200, b"[]")], "non-object response"),
            ([(200, b'{"approved": "true"}')], "did not return an approval decision"),
            ([(500, b"{}")], "returned HTTP 500"),
            ([(409, b"{}"), (200, b'{"status": "maybe"}')], "unexpected status"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                server = serve(answers)
                try:
                    with patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_URL": url_of(server)}):
                        output = await create_pre_tool_use()(pre_event(), "toolu_01", {"signal": None})
                finally:
                    server.shutdown()
                    server.server_close()
                self.assertIn(message, reason(output))


    async def test_a_mezmo_error_shows_its_code_and_message(self) -> None:
        answers = [
            (403, b'{"status":403,"code":"ENOAGENT","message":"no agent is registered for these credentials"}')
        ]
        server = serve(answers)
        try:
            with patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_URL": url_of(server)}):
                output = await create_pre_tool_use()(pre_event(), "toolu_01", {"signal": None})
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn(
            "Mezmo Governance returned HTTP 403 ENOAGENT: no agent is registered for these credentials",
            reason(output),
        )

def post_event(**overrides: Any) -> dict[str, Any]:
    event = pre_event(hook_event_name="PostToolUse", tool_response={"content": [{"type": "text", "text": "ok"}]})
    event.update(overrides)
    return event


class PostToolUseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.log = Path(self.directory.name) / "audit.jsonl"
        self.addCleanup(self.directory.cleanup)

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    async def test_writes_one_audit_line_with_the_call_identity(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(self.log)}):
            output = await post_tool_use(post_event(), "toolu_01", {"signal": None})
        self.assertEqual(output, {})
        [line] = self.lines()
        self.assertEqual(line["agent"], "claude_agent_sdk")
        self.assertEqual(line["request_id"], "claude_agent_sdk:sess-1:toolu_01#" + NOTE_DIGEST)
        self.assertEqual((line["tool_namespace"], line["tool_name"]), ("notes", "delete_note"))
        self.assertEqual(line["status"], "observed")
        self.assertIn('"ok"', line["result_preview"])

    async def test_marks_failures(self) -> None:
        failure = post_event(hook_event_name="PostToolUseFailure", error="no such note")
        del failure["tool_response"]
        error_result = post_event(tool_response={"content": [], "is_error": True})
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(self.log)}):
            await post_tool_use(failure, "toolu_01", {"signal": None})
            await post_tool_use(error_result, "toolu_02", {"signal": None})
        first, second = self.lines()
        self.assertEqual((first["status"], first["result_preview"]), ("failed", '"no such note"'))
        self.assertEqual(second["status"], "failed")

    async def test_no_tool_use_id_writes_null_ids(self) -> None:
        event = post_event()
        del event["tool_use_id"]
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(self.log)}):
            await post_tool_use(event, None, {"signal": None})
        [line] = self.lines()
        self.assertIsNone(line["decision_id"])
        self.assertIsNone(line["request_id"])

    async def test_only_the_owner_can_read_a_new_log_and_its_folder(self) -> None:
        log = Path(self.directory.name) / "fresh" / "audit.jsonl"
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(log)}):
            await post_tool_use(post_event(), "toolu_01", {"signal": None})
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)

    async def test_a_readable_log_from_an_older_version_is_tightened(self) -> None:
        self.log.write_text("", encoding="utf-8")
        self.log.chmod(0o644)
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(self.log)}):
            await post_tool_use(post_event(), "toolu_01", {"signal": None})
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.lines()), 1)

    async def test_a_log_sent_to_dev_null_writes_without_a_warning(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": os.devnull}):
            with patch("sys.stderr") as stderr:
                await post_tool_use(post_event(), "toolu_01", {"signal": None})
        self.assertEqual(stderr.write.call_args_list, [])

    async def test_an_unwritable_log_never_blocks(self) -> None:
        blocker = Path(self.directory.name) / "file"
        blocker.write_text("", encoding="utf-8")
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(blocker / "audit.jsonl")}):
            with patch("sys.stderr") as stderr:
                output = await post_tool_use(post_event(), "toolu_01", {"signal": None})
        self.assertEqual(output, {})
        self.assertIn("could not write the local audit log", "".join(c.args[0] for c in stderr.write.call_args_list))


@unittest.skipUnless(HookMatcher is not None, "claude-agent-sdk is not installed")
class RegistrationTests(unittest.TestCase):
    def test_every_tool_event_is_registered_without_a_matcher(self) -> None:
        hooks = mezmo_governance_hooks()
        self.assertEqual(set(hooks), {"PreToolUse", "PostToolUse", "PostToolUseFailure"})
        for event, matchers in hooks.items():
            with self.subTest(event=event):
                [matcher] = matchers
                self.assertIsInstance(matcher, HookMatcher)
                self.assertIsNone(matcher.matcher)
                self.assertEqual(len(matcher.hooks), 1)
        self.assertEqual(hooks["PreToolUse"][0].timeout, PRE_TOOL_USE_TIMEOUT_SECONDS)

    def test_the_hook_timeout_outlasts_the_client_deadline(self) -> None:
        from mezmo_governance_claude_agent_sdk.client import MAX_TIMEOUT_SECONDS

        self.assertLess(MAX_TIMEOUT_SECONDS, PRE_TOOL_USE_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
