#!/usr/bin/env python3
"""Add Mezmo's MCP server to Claude Code, so Claude can query Mezmo logs and work on pipelines.

It runs the command Mezmo documents, `claude mcp add`, for a user-scope server named mezmo.
The service key ends up in Claude Code's user config (~/.claude.json), as with any server
added that way.

  add     check the service key, then add the server
  remove  remove the server
  status  whether the server is set up, and whether Claude Code can connect to it
  check   check a service key against Mezmo's MCP endpoint
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import mezmo_common as mc

MCP_URL = "https://mcp.mezmo.com/mcp"
SERVER = "mezmo"
KEY_ENV = "MEZMO_SERVICE_KEY"

EXIT_ERROR, EXIT_USAGE, EXIT_REJECTED, EXIT_EXISTS = 1, 2, 3, 4

Transport = Callable[..., Tuple[int, str]]
ClaudeRunner = Callable[[Sequence[str], Optional[str]], Tuple[int, str]]


@dataclasses.dataclass
class CheckResult:
    ok: bool
    status: Optional[int]
    detail: str


def clean_key(key: Optional[str]) -> str:
    """The key without whitespace or a pasted "Bearer " prefix."""
    key = (key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[len("bearer "):].strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("The Mezmo service key is empty or has spaces in it.")
    return key


def check_key(key: str, send: Transport = mc.http_request, url: str = MCP_URL) -> CheckResult:
    """Open an MCP session with the key. 200 means Mezmo accepted it."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mezmo-claude-code-plugin", "version": "1"},
        },
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        status, text = send("POST", url, headers, json.dumps(body).encode(), 20.0)
    except OSError as err:
        return CheckResult(False, None, f"Couldn't reach Mezmo MCP at {url}: {err}")
    if status == 200:
        return CheckResult(True, status, "Mezmo MCP accepted the service key.")
    if status in (401, 403):
        return CheckResult(False, status, f"Mezmo MCP rejected the service key (HTTP {status}).")
    return CheckResult(False, status, f"Unexpected answer from Mezmo MCP (HTTP {status}): {text.strip()[:200]}")


def parse_get(output: str) -> Dict[str, Any]:
    """Scope, status and URL from `claude mcp get`. Leaves out the headers, which hold the key."""
    fields: Dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\s+(Scope|Status|URL):\s*(.*)$", line)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
    scope_text = fields.get("scope", "").lower()
    scope = next((s for s in ("user", "project", "local") if scope_text.startswith(s)), scope_text or None)
    status = fields.get("status", "")
    return {"scope": scope, "status": status, "connected": "connected" in status.lower(), "url": fields.get("url")}


def server_info(claude: ClaudeRunner) -> Optional[Dict[str, Any]]:
    """The server named mezmo, as Claude Code sees it, or None when there isn't one."""
    code, output = claude(["mcp", "get", SERVER], None)
    return parse_get(output) if code == 0 else None


def _is_mezmo(info: Mapping[str, Any]) -> bool:
    return (info.get("url") or "").rstrip("/").lower() == MCP_URL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp_config.py", description="Add Mezmo's MCP server to Claude Code.")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Check the service key, then add the server")
    add.add_argument("--key", help=f"Mezmo service key (default: ${KEY_ENV})")
    add.add_argument("--force", action="store_true", help="Replace a user-scope server that's already called mezmo")
    sub.add_parser("remove", help="Remove the server")
    status = sub.add_parser("status", help="Whether the server is set up and connects")
    status.add_argument("--json", action="store_true")
    check = sub.add_parser("check", help="Check a service key; adds nothing")
    check.add_argument("--key", help=f"Mezmo service key (default: ${KEY_ENV})")
    return parser


def _key(args_key: Optional[str], environ: Mapping[str, str]) -> str:
    key = args_key or environ.get(KEY_ENV)
    if not key:
        raise ValueError(f"No service key. Pass --key or set {KEY_ENV}.")
    return clean_key(key)


def _describe(info: Mapping[str, Any]) -> str:
    where = f"{info.get('url')}, {info.get('scope') or 'unknown'} scope"
    return f"{where}: {info.get('status') or 'status unknown'}"


def main(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    send: Transport = mc.http_request,
    claude: ClaudeRunner = mc.run_claude,
) -> int:
    environ = os.environ if environ is None else environ
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check":
            key = _key(args.key, environ)
            result = check_key(key, send)
            print(f"{'ok  ' if result.ok else 'FAIL'}  {MCP_URL}  key {mc.mask(key)}  {result.detail}")
            return 0 if result.ok else EXIT_REJECTED

        if args.command == "add":
            key = _key(args.key, environ)
            result = check_key(key, send)
            if not result.ok:
                print(f"Not added. {result.detail}", file=sys.stderr)
                return EXIT_REJECTED
            print(result.detail)
            existing = server_info(claude)
            if existing is not None:
                if not args.force or existing["scope"] != "user":
                    hint = (
                        "Run again with --force to replace it."
                        if existing["scope"] == "user"
                        else "It comes from a project's .mcp.json or a local setting, so remove it there first."
                    )
                    print(f"Claude Code already has an MCP server called {SERVER} ({_describe(existing)}). {hint}",
                          file=sys.stderr)
                    return EXIT_EXISTS
                claude(["mcp", "remove", SERVER, "-s", "user"], None)
            code, output = claude(
                ["mcp", "add", "--scope", "user", "--transport", "http", SERVER, MCP_URL,
                 "--header", f"Authorization: Bearer {key}"],
                None,
            )
            if code != 0:
                print(f"Error: claude mcp add failed: {output.replace(key, mc.mask(key)).strip()[:300]}",
                      file=sys.stderr)
                return EXIT_ERROR
            info = server_info(claude) or {}
            print(f"Added the {SERVER} MCP server ({MCP_URL}) with service key {mc.mask(key)} for all your projects.")
            print(f"Claude Code says: {info.get('status') or 'status unknown'}.")
            print("It's available in new Claude Code sessions. Its tools appear as mcp__mezmo__<tool>.")
            return 0

        if args.command == "remove":
            existing = server_info(claude)
            if existing is None:
                print(f"Claude Code has no MCP server called {SERVER}, so there's nothing to remove.")
                return 0
            if existing["scope"] != "user":
                print(f"The {SERVER} MCP server comes from {existing['scope']} config, not user config. "
                      "Remove it where it was added.", file=sys.stderr)
                return EXIT_EXISTS
            code, output = claude(["mcp", "remove", SERVER, "-s", "user"], None)
            if code != 0:
                print(f"Error: claude mcp remove failed: {output.strip()[:300]}", file=sys.stderr)
                return EXIT_ERROR
            print(f"Removed the {SERVER} MCP server from user config. New sessions won't have it.")
            return 0

        if args.command == "status":
            info = server_info(claude)
            report = {
                "configured": info is not None,
                "scope": (info or {}).get("scope"),
                "url": (info or {}).get("url"),
                "is_mezmo": bool(info) and _is_mezmo(info),
                "connected": bool(info) and info["connected"],
                "status": (info or {}).get("status"),
            }
            if args.json:
                print(json.dumps(report, indent=2))
            elif info is None:
                print("Mezmo MCP isn't set up. Run /mezmo:setup mcp to add it.")
            else:
                name = "Mezmo MCP" if report["is_mezmo"] else f"An MCP server called {SERVER}"
                print(f"{name}: {_describe(info)}")
            return 0
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
