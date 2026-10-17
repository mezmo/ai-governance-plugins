"""Claude Agent SDK hook callbacks for Mezmo exact-tool-call governance."""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mezmo_governance_claude_agent_sdk.client import (
    AGENT,
    GovernanceError,
    HttpJson,
    approval_payload,
    authorize,
    call_identity,
    httpx_json,
    required_string,
    split_tool_name,
)

# Claude Code cancels a callback at its matcher timeout and skips the tool. The
# client's 590 second deadline ends first, so the agent sees Mezmo's reason.
PRE_TOOL_USE_TIMEOUT_SECONDS = 600
POST_TOOL_USE_TIMEOUT_SECONDS = 15
DEFAULT_AUDIT_LOG = Path.home() / ".mezmo" / "governance-audit.jsonl"
MAX_RESULT_PREVIEW_CHARACTERS = 2_000

HookCallback = Callable[[Any, Any, Any], Awaitable[dict[str, Any]]]


def deny_output(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def tool_use_id_from(input_data: Mapping[str, Any], tool_use_id: str | None) -> str | None:
    for value in (tool_use_id, input_data.get("tool_use_id")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def hook_payload(input_data: Any, tool_use_id: str | None) -> dict[str, Any]:
    if not isinstance(input_data, Mapping):
        raise GovernanceError("hook input must be a JSON object")
    arguments = input_data.get("tool_input")
    if not isinstance(arguments, Mapping):
        raise GovernanceError("tool_input must be a JSON object")
    # A call without an id gets a random one, so it never reuses a decision.
    call_id = tool_use_id_from(input_data, tool_use_id) or f"generated-{uuid.uuid4()}"
    return approval_payload(
        session_id=required_string(input_data.get("session_id"), "session_id"),
        tool_call_id=call_id,
        tool_name=required_string(input_data.get("tool_name"), "tool_name"),
        arguments=arguments,
    )


def audit_log_path() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_AUDIT_LOG")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_AUDIT_LOG


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
    # The log keeps tool results, so only its owner may read it.
    path = audit_log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        # A log an older version wrote keeps its mode until narrowed here.
        mode = os.fstat(descriptor).st_mode
        if os.name != "nt" and stat.S_ISREG(mode) and mode & 0o077:
            os.fchmod(descriptor, 0o600)
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def audit_record(input_data: Mapping[str, Any], tool_use_id: str | None) -> dict[str, Any]:
    namespace, name = split_tool_name(required_string(input_data.get("tool_name"), "tool_name"))
    call_id = tool_use_id_from(input_data, tool_use_id)
    session_id = input_data.get("session_id")
    arguments = input_data.get("tool_input")
    decision_id = request_id = None
    # The identity covers the arguments, so it needs them to join the decision.
    if call_id and isinstance(session_id, str) and session_id.strip() and isinstance(arguments, Mapping):
        decision_id, request_id = call_identity(
            session_id.strip(), call_id, namespace, name, arguments
        )
    response = input_data.get("tool_response")
    failed = (
        input_data.get("hook_event_name") == "PostToolUseFailure"
        or result_has_known_error(response)
    )
    error = input_data.get("error")
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": AGENT,
        "decision_id": decision_id,
        "request_id": request_id,
        "tool_namespace": namespace,
        "tool_name": name,
        "status": "failed" if failed else "observed",
        "result_preview": result_preview(error if failed and error is not None else response),
    }


def create_pre_tool_use(
    endpoint: str | None = None,
    *,
    access_key: str | None = None,
    http: HttpJson | None = None,
) -> HookCallback:
    """Build the PreToolUse callback. ``endpoint`` and ``access_key`` win over the variables."""

    async def transport(
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        return await httpx_json(method, url, payload, timeout, access_key=access_key)

    async def pre_tool_use(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        # Any failure before a decision blocks the call: a callback that raises
        # hands the SDK an error instead of a deny.
        try:
            decision = await authorize(
                hook_payload(input_data, tool_use_id),
                http=http or transport,
                url=endpoint,
            )
        except Exception as error:
            return deny_output(
                "Mezmo Governance could not authorize this exact tool call, so "
                f"the Claude Agent SDK blocked it: {error}"
            )
        if decision.approved:
            # Empty output: the SDK's own permission rules and mode still apply.
            return {}
        detail = decision.reason or "Policy denied the call."
        return deny_output(f"Mezmo Governance denied this exact tool call: {detail}")

    return pre_tool_use


async def post_tool_use(
    input_data: Any, tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Append one audit line after a tool runs. Never blocks."""
    try:
        if not isinstance(input_data, Mapping):
            raise GovernanceError("hook input must be a JSON object")
        append_audit(audit_record(input_data, tool_use_id))
    except Exception as error:
        print(
            f"Mezmo Governance could not write the local audit log: {error}",
            file=sys.stderr,
        )
    return {}


def mezmo_governance_hooks(
    endpoint: str | None = None,
    *,
    access_key: str | None = None,
    http: HttpJson | None = None,
) -> dict[str, list[Any]]:
    """Hooks for ``ClaudeAgentOptions(hooks=...)``, with no matcher, so every tool is checked."""
    from claude_agent_sdk import HookMatcher

    pre = create_pre_tool_use(endpoint, access_key=access_key, http=http)
    return {
        "PreToolUse": [HookMatcher(hooks=[pre], timeout=PRE_TOOL_USE_TIMEOUT_SECONDS)],
        "PostToolUse": [HookMatcher(hooks=[post_tool_use], timeout=POST_TOOL_USE_TIMEOUT_SECONDS)],
        "PostToolUseFailure": [
            HookMatcher(hooks=[post_tool_use], timeout=POST_TOOL_USE_TIMEOUT_SECONDS)
        ],
    }
