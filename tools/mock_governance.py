#!/usr/bin/env python3
"""A local stand-in for Mezmo Governance, for trying the plugins.

It answers the authorize endpoint from docs/protocol.md and prints each
decision. It is not a policy engine and has no authentication.

    python3 tools/mock_governance.py --deny Bash --deny 'mcp__*'
    export MEZMO_GOVERNANCE_URL=http://127.0.0.1:8787/governance/workflow/authorize
    export MEZMO_GOVERNANCE_TOKEN=local-test

Patterns match the tool name as the plugin sends it (`Bash`, `pods_delete`)
or `<tool_namespace>:<tool_name>` (`kubernetes:pods_delete`), with fnmatch
wildcards. Everything not denied or held is approved.
"""

from __future__ import annotations

import argparse
import json
import sys
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PATH = "/governance/workflow/authorize"


def decide(item: dict, deny: list[str], hold: list[str]) -> str:
    name = str(item.get("tool_name", ""))
    qualified = f"{item.get('tool_namespace', 'default')}:{name}"

    def matches(patterns: list[str]) -> bool:
        return any(fnmatchcase(name, p) or fnmatchcase(qualified, p) for p in patterns)

    if matches(deny):
        return "denied"
    if matches(hold):
        return "pending"
    return "approved"


def make_handler(deny: list[str], hold: list[str]) -> type[BaseHTTPRequestHandler]:
    decisions: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            if self.headers.get("Authorization", "").strip():
                return True
            self.reply(401, {"error": "missing Authorization header"})
            return False

        def do_POST(self) -> None:
            if urlparse(self.path).path != PATH:
                return self.reply(404, {})
            if not self.authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length))
                item = payload["items"][0]
                decision_id = str(payload["decision_id"])
            except (ValueError, KeyError, IndexError, TypeError):
                return self.reply(400, {"error": "not an authorize request"})
            if decision_id in decisions:
                return self.reply(409, {"error": "decision already open"})
            status = decide(item, deny, hold)
            decisions[decision_id] = status
            label = f"{item.get('tool_namespace')}:{item.get('tool_name')}"
            # Tool arguments can hold secrets, so print only the decision.
            # One write per line: parallel tool calls arrive on parallel threads.
            sys.stdout.write(f"{status:8} {label}\n")
            sys.stdout.flush()
            if status == "pending":
                # Like Mezmo: 207 means sent for approval; the plugin then polls.
                return self.reply(207, {"approved": False, "reason": "sent for approval"})
            reason = f"mock policy: {label} is denied" if status == "denied" else None
            self.reply(200, {"approved": status == "approved", "reason": reason})

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != PATH:
                return self.reply(404, {})
            if not self.authorized():
                return
            decision_id = parse_qs(url.query).get("decision_id", [""])[0]
            if decision_id not in decisions:
                return self.reply(404, {"error": "unknown decision_id"})
            status = decisions[decision_id]
            # The mock never decides a held call, so it stays 207 until the plugin gives up.
            self.reply(207 if status == "pending" else 200, {"approved": status == "approved", "reason": None})

        def log_message(self, *args: object) -> None:
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--deny", action="append", default=[], metavar="PATTERN")
    parser.add_argument(
        "--hold",
        action="append",
        default=[],
        metavar="PATTERN",
        help="leave matching calls pending, to see the plugin wait and time out",
    )
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.deny, args.hold))
    url = f"http://127.0.0.1:{server.server_port}{PATH}"
    print(f"Mock Mezmo Governance on {url}", flush=True)
    print(f"  export MEZMO_GOVERNANCE_URL={url}", flush=True)
    print("  export MEZMO_GOVERNANCE_TOKEN=local-test", flush=True)
    print(f"  deny: {args.deny or '-'}  hold: {args.hold or '-'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
