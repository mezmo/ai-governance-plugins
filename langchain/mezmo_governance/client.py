"""Mezmo Governance authorize client used by the LangChain middleware."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

AGENT = "langchain"
DEFAULT_TIMEOUT_SECONDS = 590.0
# Finish before a runtime's 600 second hook timeout, whatever is configured.
MAX_TIMEOUT_SECONDS = 590.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MIN_POLL_INTERVAL_SECONDS = 1.0
MCP_TOOL_PREFIX = "mcp__"

HttpJson = Callable[
    [str, str, Mapping[str, Any] | None, float],
    Awaitable[tuple[int, dict[str, Any]]],
]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class GovernanceError(RuntimeError):
    pass


class Decision(NamedTuple):
    approved: bool
    reason: str | None


def required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceError(f"{field} must be a non-empty string")
    return value.strip()


def authorize_url() -> str:
    return required_string(os.environ.get("MEZMO_GOVERNANCE_URL"), "MEZMO_GOVERNANCE_URL")


def authorization_header(access_key: str | None = None) -> str:
    key = access_key or os.environ.get("MEZMO_GOVERNANCE_ACCESS_KEY")
    key = required_string(key, "MEZMO_GOVERNANCE_ACCESS_KEY")
    if key.lower().startswith("token ") or key.lower().startswith("bearer "):
        return key
    return f"Token {key}"


def positive_seconds(name: str, default: float) -> float:
    configured = os.environ.get(name)
    if configured is None:
        return default
    try:
        timeout = float(configured)
    except ValueError as error:
        raise GovernanceError(f"{name} must be a number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise GovernanceError(f"{name} must be a finite number greater than zero")
    return timeout


def timeout_seconds() -> float:
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


def status_url(decision_id: str, base: str | None = None) -> str:
    parsed = urlparse(base or authorize_url())
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["decision_id"] = [decision_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def split_tool_name(tool_name: str) -> tuple[str, str]:
    if tool_name.startswith(MCP_TOOL_PREFIX):
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise GovernanceError("MCP tool names must use mcp__<server>__<tool>")
        return parts[1], parts[2]
    return AGENT, tool_name


def call_identity(session_id: str, tool_call_id: str) -> tuple[str, str]:
    request_id = f"{AGENT}:{session_id}:{tool_call_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, request_id)), request_id


def approval_payload(
    *,
    session_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    matched_pattern: str | None = None,
    intent: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    namespace, name = split_tool_name(tool_name)
    generated_id, request_id = call_identity(session_id, tool_call_id)
    origin: dict[str, Any] = {
        "kind": "langchain_middleware",
        "reason": "awrap_tool_call",
    }
    if matched_pattern:
        origin["matched_pattern"] = matched_pattern
    item: dict[str, Any] = {
        "id": 0,
        "tool_namespace": namespace,
        "tool_name": name,
        "arguments": dict(arguments),
    }
    if intent:
        item["tool_call_intent"] = intent
    return {
        "version": 1,
        "decision_id": decision_id or generated_id,
        "request_id": request_id,
        "scope": {"kind": "single", "session_id": session_id},
        "origin": origin,
        "items": [item],
    }


async def httpx_json(
    method: str,
    url: str,
    payload: Mapping[str, Any] | None,
    timeout: float,
    *,
    access_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    import httpx

    headers = {
        "Authorization": authorization_header(access_key),
        "Accept": "application/json",
    }

    async def request() -> tuple[int, Any]:
        # A 3xx comes back as itself and denies, so the key never goes elsewhere.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method,
                url,
                json=None if payload is None else dict(payload),
                headers=headers,
            )
            try:
                return response.status_code, response.json()
            except ValueError as error:
                raise GovernanceError(
                    "Mezmo Governance returned an invalid JSON response"
                ) from error

    try:
        # httpx timeouts apply to each read; wait_for bounds the whole response,
        # so a server that drips its body cannot outlast the deadline.
        status, body = await asyncio.wait_for(request(), timeout)
    except (httpx.TimeoutException, asyncio.TimeoutError) as error:
        raise TimeoutError(str(error) or "Mezmo Governance request timed out") from error
    except httpx.HTTPError as error:
        raise GovernanceError(f"Mezmo Governance is unavailable: {error}") from error
    if not isinstance(body, dict):
        raise GovernanceError("Mezmo Governance returned a non-object response")
    return status, body


def remaining_seconds(deadline: float, clock: Clock) -> float:
    leftover = deadline - clock()
    if leftover <= 0:
        raise GovernanceError("Mezmo Governance: approval still pending")
    return leftover


def in_time(deadline: float, clock: Clock) -> None:
    """Refuse a response that lands after the deadline, even an approval."""
    if clock() >= deadline:
        raise GovernanceError("Mezmo Governance answered after the deadline")


def decision_from_post(body: Mapping[str, Any]) -> Decision:
    approved = body.get("approved")
    if not isinstance(approved, bool):
        raise GovernanceError("Mezmo Governance did not return an approval decision")
    reason = body.get("reason")
    return Decision(approved, reason if isinstance(reason, str) else None)


def decision_from_status(body: Mapping[str, Any]) -> Decision | None:
    status = body.get("status")
    reason = body.get("reason")
    detail = reason if isinstance(reason, str) else None
    if status == "approved":
        return Decision(True, detail)
    if status == "denied":
        return Decision(False, detail or "Policy denied the call.")
    if status == "pending":
        return None
    raise GovernanceError(f"Mezmo Governance returned an unexpected status: {status!r}")


async def poll_decision(
    decision_id: str,
    deadline: float,
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
    base_url: str | None = None,
) -> Decision:
    url = status_url(decision_id, base_url)
    interval = poll_interval_seconds()
    while True:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = await http("GET", url, None, leftover)
        except TimeoutError as error:
            raise GovernanceError(f"Mezmo Governance is unavailable: {error}") from error
        in_time(deadline, clock)
        if status == 404:
            await sleep(min(interval, leftover))
            continue
        if status != 200:
            raise GovernanceError(
                f"Mezmo Governance returned HTTP {status} while polling"
            )
        decided = decision_from_status(body)
        if decided is not None:
            return decided
        leftover = remaining_seconds(deadline, clock)
        await sleep(min(interval, leftover))


async def authorize(
    payload: Mapping[str, Any],
    *,
    http: HttpJson | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleeper | None = None,
    url: str | None = None,
) -> Decision:
    """Ask Mezmo about one call. ``url`` overrides ``MEZMO_GOVERNANCE_URL``."""
    http_fn = http or httpx_json
    sleep_fn = sleep or asyncio.sleep
    base_url = url or authorize_url()
    deadline = clock() + timeout_seconds()
    leftover = remaining_seconds(deadline, clock)
    try:
        status, body = await http_fn("POST", base_url, payload, leftover)
    except TimeoutError:
        return await poll_decision(
            str(payload["decision_id"]),
            deadline,
            http_fn,
            clock,
            sleep_fn,
            base_url,
        )
    in_time(deadline, clock)
    if status == 409:
        return await poll_decision(
            str(payload["decision_id"]),
            deadline,
            http_fn,
            clock,
            sleep_fn,
            base_url,
        )
    if status != 200:
        raise GovernanceError(f"Mezmo Governance returned HTTP {status}")
    return decision_from_post(body)
