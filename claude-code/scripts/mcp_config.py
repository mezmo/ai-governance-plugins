#!/usr/bin/env python3
"""Add Mezmo's MCP server to Claude Code, so Claude can query Mezmo logs and work on pipelines.

It runs the command Mezmo documents, `claude mcp add`, for a user-scope server named mezmo.
The service key ends up in Claude Code's user config (~/.claude.json), as with any server
added that way. The key is in that child command's arguments while it runs: `claude mcp
add-json -` reads the server from stdin instead, but Claude Code refuses that inside a
Claude Code session, which is where setup runs.

The server is named exactly mezmo, since the investigate skill allows its
mcp__mezmo__get_ledger_investigation tool. The URL is Mezmo's production MCP server unless
--url names another; governance_config.py configure connects it too, from the same key.

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
import urllib.parse
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

import mezmo_common as mc

MCP_URL = "https://mcp.mezmo.com/mcp"
# The API URL whose MCP server is MCP_URL. Any other API URL needs its MCP URL given.
PRODUCTION_API_URL = "https://api.mezmo.com"
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
    """The key without whitespace or a pasted "Bearer " or "Token " prefix."""
    key = (key or "").strip()
    for prefix in ("bearer ", "token "):
        if key.lower().startswith(prefix):
            key = key[len(prefix):].strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("The Mezmo service key is empty or has spaces in it.")
    return key


def mcp_url(value: str) -> str:
    """`value` as an MCP URL: https://, or http:// to this machine, with no user name or password."""
    parsed = urllib.parse.urlparse((value or "").strip())
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and mc.is_loopback(parsed.hostname))
    if not secure or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"the Mezmo MCP URL must be an https:// URL with no user name or password, such as {MCP_URL}")
    return value.strip().rstrip("/")


def url_for_api(api_url: str) -> Optional[str]:
    """MCP_URL for the production API URL; None for any other, whose MCP URL must be given."""
    return MCP_URL if api_url.strip().rstrip("/").lower() == PRODUCTION_API_URL else None


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


def _is_mezmo(info: Mapping[str, Any], url: str = MCP_URL) -> bool:
    return (info.get("url") or "").rstrip("/").lower() in (MCP_URL, url.rstrip("/").lower())


def connect(
    key: str, url: str, claude: ClaudeRunner, send: Transport = mc.http_request, *, force: bool, check: bool = True
) -> int:
    """Check the key against `url`, then add it as the user-scope server mezmo. Prints what it did.

    With `force`, a user-scope server called mezmo is replaced. One from a project or local config
    never is. Returns 0, EXIT_REJECTED, EXIT_EXISTS or EXIT_ERROR. Never prints the key.
    """
    if check:
        result = check_key(key, send, url)
        if not result.ok:
            print(f"Not added. {result.detail}", file=sys.stderr)
            return EXIT_REJECTED
        print(result.detail)
    existing = server_info(claude)
    if existing is not None:
        if not force or existing["scope"] != "user":
            hint = (
                "Run again with --force to replace it."
                if existing["scope"] == "user"
                else "It comes from a project's .mcp.json or a local setting, so remove it there first."
            )
            print(f"Claude Code already has an MCP server called {SERVER} ({_describe(existing)}). {hint}",
                  file=sys.stderr)
            return EXIT_EXISTS
        claude(["mcp", "remove", SERVER, "-s", "user"], None)
        print(f"Replaced the user-scope MCP server called {SERVER} ({existing.get('url')}).")
    code, output = claude(
        ["mcp", "add", "--scope", "user", "--transport", "http", SERVER, url,
         "--header", f"Authorization: Bearer {key}"],
        None,
    )
    if code != 0:
        print(f"Error: claude mcp add failed: {output.replace(key, mc.mask(key)).strip()[:300]}", file=sys.stderr)
        return EXIT_ERROR
    info = server_info(claude) or {}
    print(f"Added the {SERVER} MCP server ({url}) with service key {mc.mask(key)} for all your projects.")
    print(f"Claude Code says: {info.get('status') or 'status unknown'}.")
    print("It's available in new Claude Code sessions. Its tools appear as mcp__mezmo__<tool>.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp_config.py", description="Add Mezmo's MCP server to Claude Code.")
    sub = parser.add_subparsers(dest="command", required=True)
    url_help = f"Mezmo MCP URL (default: {MCP_URL})"
    add = sub.add_parser("add", help="Check the service key, then add the server")
    mc.add_key_arguments(add, f"Mezmo service key (default: ${KEY_ENV})")
    mc.add_saved_key_argument(add)
    add.add_argument("--url", default=MCP_URL, help=url_help)
    add.add_argument("--force", action="store_true", help="Replace a user-scope server that's already called mezmo")
    sub.add_parser("remove", help="Remove the server")
    status = sub.add_parser("status", help="Whether the server is set up and connects")
    status.add_argument("--json", action="store_true")
    status.add_argument("--url", default=MCP_URL, help=f"The Mezmo MCP URL to expect (default: {MCP_URL})")
    check = sub.add_parser("check", help="Check a service key; adds nothing")
    mc.add_key_arguments(check, f"Mezmo service key (default: ${KEY_ENV})")
    mc.add_saved_key_argument(check)
    check.add_argument("--url", default=MCP_URL, help=url_help)
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
            url = mcp_url(args.url)
            key = _key(mc.key_argument(args), environ)
            result = check_key(key, send, url)
            print(f"{'ok  ' if result.ok else 'FAIL'}  {url}  key {mc.mask(key)}  {result.detail}")
            return 0 if result.ok else EXIT_REJECTED

        if args.command == "add":
            url = mcp_url(args.url)
            key = _key(mc.key_argument(args), environ)
            return connect(key, url, claude, send, force=args.force)

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
                "is_mezmo": bool(info) and _is_mezmo(info, args.url),
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
    finally:
        mc.cleanup_key_file(args)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
