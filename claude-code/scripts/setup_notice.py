#!/usr/bin/env python3
"""SessionStart hook: tell the user the one step left to set up Mezmo, or say nothing.

Installing the plugin asks for the key and the API URL, which turns on governance and the ledger,
but nothing else says that Claude can read the Mezmo ledger only once the `mezmo` MCP server is
connected, or that /mezmo:setup exists at all. This names the one command to run:

- governance on, no `mezmo` MCP server: /mezmo:setup mcp;
- no key: /mezmo:setup;
- governance turned off in the plugin options, or everything set up: nothing.

It reads local files only, never the network or the `claude` CLI, and always exits 0.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import governance_hook  # noqa: E402

SERVER = "mezmo"
NO_KEY = "Mezmo Governance is off: run /mezmo:setup to turn it on."
NO_MCP = "Mezmo is on, but Claude can't read the Mezmo ledger yet: run /mezmo:setup mcp to connect it."


def _json(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_server(scope: Any) -> bool:
    servers = scope.get("mcpServers") if isinstance(scope, dict) else None
    return isinstance(servers, dict) and SERVER in servers


def mcp_configured(*, cwd: str, environ: Mapping[str, str]) -> bool:
    """Whether Claude Code has a `mezmo` MCP server in user, local or project scope."""
    folder = (environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    config = _json(Path(folder).expanduser() / ".claude.json" if folder else Path.home() / ".claude.json")
    projects = config.get("projects")
    local = projects.get(cwd) if isinstance(projects, dict) else None
    return _has_server(config) or _has_server(local) or _has_server(_json(Path(cwd) / ".mcp.json"))


def notice(*, cwd: str, environ: Mapping[str, str]) -> Optional[str]:
    """The one step left, or None. Never raises."""
    try:
        if governance_hook.load_config() is not None:
            return None if mcp_configured(cwd=cwd, environ=environ) else NO_MCP
        enabled = (environ.get(governance_hook.ENABLED_OPTION) or "").strip().lower()
        # Turned off on purpose, as the plugin options allow: say nothing.
        return None if enabled in governance_hook.FALSE_VALUES else NO_KEY
    except Exception:
        return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    cwd = event.get("cwd") if isinstance(event, dict) else None
    message = notice(cwd=cwd if isinstance(cwd, str) and cwd else os.getcwd(), environ=os.environ)
    if message is not None:
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message},
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
