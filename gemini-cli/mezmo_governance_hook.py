#!/usr/bin/env python3
"""Gemini CLI BeforeTool / AfterTool hook for Mezmo Governance."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENT = "gemini_cli"
DEFAULT_TIMEOUT_SECONDS = 590.0
MAX_TIMEOUT_SECONDS = 590.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_AUDIT_LOG = Path.home() / ".mezmo" / "governance-audit.jsonl"
MAX_RESULT_PREVIEW_CHARACTERS = 2_000

HttpJson = Callable[[str, str, Optional[Mapping[str, Any]], float], tuple[int, dict[str, Any]]]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class HookContractError(ValueError):
    pass


class NormalizedToolCall(NamedTuple):
    namespace: str
    name: str
    arguments: dict[str, Any]


class CallIdentity(NamedTuple):
    decision_id: str
    request_id: str
    session_id: str


class Decision(NamedTuple):
    approved: bool
    reason: str | None


def required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookContractError(f"{field} must be a non-empty string")
    return value.strip()


def parse_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookContractError(
            "Mezmo Governance returned an invalid JSON response"
        ) from error
    if not isinstance(parsed, dict):
        raise HookContractError("Mezmo Governance returned a non-object response")
    return parsed


def authorize_url() -> str:
    return required_string(
        os.environ.get("MEZMO_GOVERNANCE_URL"),
        "MEZMO_GOVERNANCE_URL",
    )


def status_url(decision_id: str, base: str | None = None) -> str:
    parsed = urlparse(base or authorize_url())
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["decision_id"] = [decision_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def authorization_header() -> str:
    key = required_string(
        os.environ.get("MEZMO_GOVERNANCE_TOKEN"),
        "MEZMO_GOVERNANCE_TOKEN",
    )
    if key.lower().startswith("token ") or key.lower().startswith("bearer "):
        return key
    return f"Token {key}"


def positive_seconds(environment_name: str, default: float) -> float:
    configured = os.environ.get(environment_name)
    if configured is None:
        return default
    try:
        seconds = float(configured)
    except ValueError as error:
        raise HookContractError(f"{environment_name} must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise HookContractError(
            f"{environment_name} must be a finite number greater than zero"
        )
    return seconds


def timeout_seconds() -> float:
    # The runtime kills the hook at its 600 second timeout; finish first.
    return min(
        positive_seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        MAX_TIMEOUT_SECONDS,
    )


def poll_interval_seconds() -> float:
    return max(
        positive_seconds(
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS",
            DEFAULT_POLL_INTERVAL_SECONDS,
        ),
        MIN_POLL_INTERVAL_SECONDS,
    )


def audit_log_path() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_AUDIT_LOG")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_AUDIT_LOG


def normalize_tool_call(event: Mapping[str, Any]) -> NormalizedToolCall:
    canonical_name = required_string(event.get("tool_name"), "tool_name")
    raw_input = event.get("tool_input")
    if not isinstance(raw_input, dict):
        raise HookContractError("tool_input must be a JSON object")

    # MCP tools carry mcp_context with the server and its own tool name.
    context = event.get("mcp_context")
    if isinstance(context, dict):
        server = context.get("server_name")
        tool = context.get("tool_name")
        if isinstance(server, str) and server.strip():
            name = tool.strip() if isinstance(tool, str) and tool.strip() else canonical_name
            return NormalizedToolCall(server.strip(), name, dict(raw_input))

    return NormalizedToolCall(AGENT, canonical_name, dict(raw_input))


def call_digest(namespace: str, name: str, arguments: Mapping[str, Any]) -> str:
    """Hash the exact call, so an edited call never reuses an earlier decision."""
    canonical = json.dumps(
        [namespace, name, dict(arguments)], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Mezmo answers HTTP 400 to a longer request_id. The decision_id hashes the full id,
# so a cut id still gets its own decision.
MAX_REQUEST_ID_CHARACTERS = 128


def bounded_request_id(request_id: str) -> str:
    if len(request_id) <= MAX_REQUEST_ID_CHARACTERS:
        return request_id
    head, digest = request_id.rsplit("#", 1)
    return head[: MAX_REQUEST_ID_CHARACTERS - len(digest) - 1] + "#" + digest


def call_identity(
    event: Mapping[str, Any], tool_call: NormalizedToolCall
) -> CallIdentity:
    session_id = required_string(event.get("session_id"), "session_id")
    # Gemini CLI sends no tool call id. A fresh id per run keeps a repeated
    # identical call from reusing an earlier decision through HTTP 409.
    request_id = f"{AGENT}:{session_id}:run-{uuid.uuid4()}"
    request_id += "#" + call_digest(*tool_call)
    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request_id))
    return CallIdentity(decision_id, bounded_request_id(request_id), session_id)


def origin(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "gemini_cli_hook",
        "reason": str(event.get("hook_event_name") or "Gemini CLI tool hook"),
    }


def approval_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    tool_call = normalize_tool_call(event)
    identity = call_identity(event, tool_call)
    return {
        "version": 1,
        "decision_id": identity.decision_id,
        "request_id": identity.request_id,
        "scope": {
            "kind": "single",
            "session_id": identity.session_id,
        },
        "origin": origin(event),
        "items": [
            {
                "id": 0,
                "tool_namespace": tool_call.namespace,
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
            }
        ],
    }


class RefuseRedirect(HTTPRedirectHandler):
    """Hand a 3xx back as an error, so the token never goes to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def http_json(
    method: str,
    url: str,
    payload: Mapping[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    body = (
        None
        if payload is None
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    headers = {
        "Authorization": authorization_header(),
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(RefuseRedirect).open(request, timeout=timeout) as response:
            return response.status, parse_object(response.read())
    except HTTPError as error:
        try:
            parsed = parse_object(error.read())
        except HookContractError:
            parsed = {}
        return error.code, parsed
    except TimeoutError:
        raise
    except URLError as error:
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise TimeoutError(str(error)) from error
        if "timed out" in str(error).lower():
            raise TimeoutError(str(error)) from error
        raise HookContractError(f"Mezmo Governance is unavailable: {error}") from error
    except socket.timeout as error:
        raise TimeoutError(str(error)) from error


MAX_ERROR_DETAIL_CHARACTERS = 200


def error_detail(value: Any) -> str:
    """Mezmo's own error text on one bounded line, or "" when there is none."""
    if not isinstance(value, str):
        return ""
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(printable.split())[:MAX_ERROR_DETAIL_CHARACTERS]


def http_failure(status: int, body: Mapping[str, Any], when: str = "") -> str:
    """Name the status and, on a 4xx, Mezmo's code and message; nothing else."""
    code = message = ""
    if 400 <= status < 500:
        code = error_detail(body.get("code"))
        message = error_detail(body.get("message"))
    text = f"Mezmo Governance returned HTTP {status}"
    if code:
        text += f" {code}"
    text += when
    if message:
        text += f": {message}"
    return text


def remaining_seconds(deadline: float, clock: Clock) -> float:
    leftover = deadline - clock()
    if leftover <= 0:
        raise HookContractError("Mezmo Governance: approval still pending")
    return leftover


def decision_from_post_body(body: Mapping[str, Any]) -> Decision:
    approved = body.get("approved")
    if not isinstance(approved, bool):
        raise HookContractError("Mezmo Governance did not return an approval decision")
    reason = body.get("reason")
    return Decision(approved, reason if isinstance(reason, str) else None)


def decision_from_status_body(body: Mapping[str, Any]) -> Decision | None:
    status = body.get("status")
    reason = body.get("reason")
    detail = reason if isinstance(reason, str) else None
    # Live Mezmo answers a decided poll with HTTP 200 and the POST body shape.
    if status is None and isinstance(body.get("approved"), bool):
        status = "approved" if body["approved"] else "denied"
    if status == "approved":
        return Decision(True, detail)
    if status == "denied":
        return Decision(False, detail or "Policy denied the call.")
    if status == "pending":
        return None
    raise HookContractError(
        f"Mezmo Governance returned an unexpected status: {status!r}"
    )


def poll_decision(
    decision_id: str,
    deadline: float,
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
) -> Decision:
    url = status_url(decision_id)
    interval = poll_interval_seconds()
    while True:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = http("GET", url, None, leftover)
        except TimeoutError as error:
            raise HookContractError(
                f"Mezmo Governance is unavailable: {error}"
            ) from error
        # An answer that arrives after the deadline is too late to act on.
        leftover = remaining_seconds(deadline, clock)
        # 404: not recorded yet. 207: Mezmo still waits for a person.
        if status in (404, 207):
            sleep(min(interval, leftover))
            continue
        if status != 200:
            raise HookContractError(http_failure(status, body, " while polling"))
        decided = decision_from_status_body(body)
        if decided is not None:
            return decided
        sleep(min(interval, leftover))


def deadline_watchdog(seconds: float) -> Callable[[], None]:
    """Deny and exit at the deadline, even while a socket read is still alive.

    The opener's timeout bounds each socket wait, not the whole answer, so a
    server that sends one byte at a time could keep the hook past the
    runtime's own hook timeout, and several runtimes then run the tool.
    """
    lock = threading.Lock()
    finished = False

    def fire() -> None:
        with lock:
            if finished:
                return
            reason = "Mezmo Governance: approval still pending"
            try:
                sys.stdout.write(
                    json.dumps(deny_output(reason), separators=(",", ":")) + "\n"
                )
                sys.stdout.flush()
                sys.stderr.write(reason + "\n")
                sys.stderr.flush()
            finally:
                # Every supported runtime blocks the tool on exit code 2, so
                # exit with it even when stdout is gone.
                os._exit(2)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()

    def stop() -> None:
        nonlocal finished
        with lock:
            finished = True
        timer.cancel()

    return stop


def authorize(
    payload: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> Decision:
    budget = timeout_seconds()
    deadline = clock() + budget
    url = authorize_url()
    stop_watchdog = deadline_watchdog(budget)
    try:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = http("POST", url, payload, leftover)
        except TimeoutError:
            return poll_decision(
                str(payload["decision_id"]), deadline, http, clock, sleep
            )

        # An answer that arrives after the deadline is too late to act on.
        remaining_seconds(deadline, clock)
        # 207: sent for approval; its approved: false is not the decision.
        if status in (207, 409):
            return poll_decision(
                str(payload["decision_id"]), deadline, http, clock, sleep
            )
        if status != 200:
            raise HookContractError(http_failure(status, body))
        return decision_from_post_body(body)
    finally:
        stop_watchdog()


def result_preview(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[:MAX_RESULT_PREVIEW_CHARACTERS]


def result_has_known_error(value: Any) -> bool:
    if isinstance(value, str):
        # Some runtimes send the tool result as JSON text.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or value.get("is_error") is True:
        return True
    exit_code = value.get("exit_code", value.get("exitCode"))
    return isinstance(exit_code, int) and exit_code != 0


def append_audit(record: Mapping[str, Any]) -> None:
    path = audit_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":"))
                     + "\n")


def handle_pre_tool_use(
    event: Mapping[str, Any],
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
) -> dict[str, Any] | None:
    decision = authorize(approval_payload(event), http=http, clock=clock, sleep=sleep)
    if decision.approved:
        return None
    detail = decision.reason or "Policy denied the call."
    return deny_output(f"Mezmo Governance denied this exact tool call: {detail}")


def handle_post_tool_use(event: Mapping[str, Any]) -> None:
    tool_call = normalize_tool_call(event)
    response = event.get("tool_response")
    failed = result_has_known_error(response) or (
        isinstance(response, dict) and bool(response.get("error"))
    )
    append_audit(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": AGENT,
            # Without a tool call id the record cannot be joined to its decision.
            "decision_id": None,
            "request_id": None,
            "tool_namespace": tool_call.namespace,
            "tool_name": tool_call.name,
            "status": "failed" if failed else "observed",
            "result_preview": result_preview(response),
        }
    )


def deny_output(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def handle_event(
    event: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any] | None:
    event_name = event.get("hook_event_name")
    if event_name == "BeforeTool":
        try:
            return handle_pre_tool_use(event, http, clock, sleep)
        except Exception as error:
            return deny_output(
                "Mezmo Governance could not authorize this exact tool call, so "
                f"Gemini CLI blocked it: {error}"
            )
    if event_name == "AfterTool":
        try:
            handle_post_tool_use(event)
        except Exception as error:
            print(
                f"Mezmo Governance could not write the local audit log: {error}",
                file=sys.stderr,
            )
        return None
    raise HookContractError(f"unsupported hook event: {event_name!r}")


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise HookContractError("hook input must be a JSON object")
        output = handle_event(event)
    except (HookContractError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2

    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
