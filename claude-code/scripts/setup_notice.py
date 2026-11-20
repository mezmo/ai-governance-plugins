#!/usr/bin/env python3
"""SessionStart and UserPromptSubmit hook: say what's missing from the Mezmo setup, once per session.

Installing the plugin with a key turns on governance, the ledger and Mezmo MCP (the plugin brings
the `mezmo` server), with no restart. No hook runs on install, so a plugin installed mid-session
speaks first on the next prompt, and one installed before the session at its start. It names:

- no key: governance, the ledger and Mezmo MCP are all off, and how to add it;
- a `mezmo` server an earlier setup added to Claude Code's user or local config: it duplicates the
  plugin's tools, and how to remove it.

It says nothing when governance is turned off in the plugin options (and no key is set), or when
nothing is missing. It reads local files only, never the network or the `claude` CLI, and always
exits 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import governance_hook  # noqa: E402

SERVER = "mezmo"
NO_KEY = (
    "Mezmo is installed but has no service key, so Mezmo Governance, the Mezmo ledger and Mezmo MCP are off. "
    "Run /plugin configure mezmo@mezmo to add it, then /reload-plugins."
)
OLD_SERVER = (
    "Claude Code has a mezmo MCP server an earlier Mezmo setup added, and the plugin now brings its own, so "
    "their tools are duplicated. Remove the old one: claude mcp remove mezmo -s {scope}"
)
# Markers of sessions already told, kept this long.
MARKER_SECONDS = 7 * 24 * 3600


def _json(path: Path) -> Mapping[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _has_server(scope: Any) -> bool:
    servers = scope.get("mcpServers") if isinstance(scope, dict) else None
    return isinstance(servers, dict) and SERVER in servers


def old_server_scope(*, cwd: str, environ: Mapping[str, str]) -> Optional[str]:
    """"user" or "local" when Claude Code's own config has a server called mezmo, else None."""
    folder = (environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    config = _json(Path(folder).expanduser() / ".claude.json" if folder else Path.home() / ".claude.json")
    if _has_server(config):
        return "user"
    projects = config.get("projects")
    return "local" if _has_server(projects.get(cwd) if isinstance(projects, dict) else None) else None


def notice(*, cwd: str, environ: Mapping[str, str]) -> Optional[str]:
    """What's missing, or None. Never raises."""
    try:
        if governance_hook.load_config() is None and governance_hook.option_key() is None:
            enabled = (environ.get(governance_hook.ENABLED_OPTION) or "").strip().lower()
            # Turned off on purpose, as the plugin options allow: say nothing.
            return None if enabled in governance_hook.FALSE_VALUES else NO_KEY
        scope = old_server_scope(cwd=cwd, environ=environ)
        return OLD_SERVER.format(scope=scope) if scope else None
    except Exception:
        return None


def first_time(session_id: Any) -> bool:
    """Whether this session hasn't been told yet, marking it told. A session without an id is told each time."""
    if not isinstance(session_id, str) or not session_id.strip():
        return True
    try:
        folder = governance_hook.state_dir() / "notices"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = folder / hashlib.sha256(session_id.strip().encode("utf-8")).hexdigest()[:16]
        if marker.exists():
            return False
        marker.touch()
        cutoff = time.time() - MARKER_SECONDS
        for old in folder.iterdir():
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        return True
    except OSError:
        return True


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}
    event = event if isinstance(event, dict) else {}
    cwd = event.get("cwd")
    message = notice(cwd=cwd if isinstance(cwd, str) and cwd else os.getcwd(), environ=os.environ)
    if message is not None and first_time(event.get("session_id")):
        name = event.get("hook_event_name")
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": name if name in ("SessionStart", "UserPromptSubmit") else "SessionStart",
                "additionalContext": message,
            },
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
