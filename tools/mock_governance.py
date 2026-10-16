#!/usr/bin/env python3
"""A local stand-in for Mezmo Governance, for trying the plugins.

It answers the authorize endpoint from docs/protocol.md and prints each
decision. It is not a policy engine and has no authentication. It also
answers the policy calls `./mezmo-governance policy` makes, from one agent
and one policy kept in memory.

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
import uuid
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PATH = "/governance/workflow/authorize"
API = "/v3/governance"
RULE_FIELDS = {"rule_id", "name", "tool", "namespace", "args", "recourse", "code", "error_message"}
BODY_FIELDS = {"name", "description", "default_action", "approver_role_id", "hitl_approval_channels", "rules"}
RECOURSES = {"allow", "deny", "approval_required"}


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


def object_id() -> str:
    return uuid.uuid4().hex[:24]


def compiled(rule: dict) -> dict:
    """A rule as the policy API returns it, from the flat rule a draft write sends."""
    definition = {key: value for key, value in rule.items() if key != "rule_id"}
    return {"rule_id": rule.get("rule_id") or object_id(), "recourse": rule["recourse"], "definition": definition, "warnings": []}


def governance_state() -> dict:
    """One agent on one policy with one rule. Tests change it in place: `fail` maps a method to the status it gets."""
    policy_id = object_id()
    probe = compiled({"recourse": "allow", "namespace": {"op": "eq", "value": "doctor"}, "tool": {"op": "eq", "value": "probe"}})
    return {
        "agents": [
            {"agent_id": object_id(), "name": "Claude Code on example.test", "runtime_name": "claude-code", "policy_id": policy_id}
        ],
        "policies": [
            {
                "policy_id": policy_id,
                "name": "Mock policy",
                "description": "A stand-in for trying policy show and apply",
                "default_action": {
                    "recourse": "approval_required",
                    "code": "ERR_NO_MATCHING_RULE",
                    "error_message": "No rule in this policy covers this request.",
                },
                "draft": None,
                "rules": [probe],
            }
        ],
        "drafts": {},
        "fail": {},
        "calls": [],
    }


def draft_problem(body: object, draft: dict) -> str | None:
    """What the policy API would refuse in a draft write, or None."""
    if not isinstance(body, dict) or set(body) - BODY_FIELDS:
        return "the body has a field a draft does not take"
    if not body.get("name") or not isinstance(body.get("default_action"), dict):
        return "name and default_action are required"
    known = {rule["rule_id"] for rule in draft["rules"]}
    for rule in body.get("rules", []):
        if not isinstance(rule, dict) or set(rule) - RULE_FIELDS:
            return "a rule has a field a rule does not take"
        if rule.get("recourse") not in RECOURSES:
            return "a rule's recourse is not allow, deny or approval_required"
        if "rule_id" in rule and rule["rule_id"] not in known:
            return f"rule_id {rule['rule_id']} is not in this draft"
        for field in ("tool", "namespace"):
            condition = rule.get(field, {"value": "x"})
            op, value = condition.get("op", "eq"), condition.get("value")
            names = isinstance(value, list) and value and all(isinstance(item, str) for item in value)
            if not ((op in ("eq", "neq") and isinstance(value, str)) or (op in ("in", "nin") and names)):
                return f"a rule's {field} is not eq/neq with a name or in/nin with a list of names"
    return None


def make_handler(deny: list[str], hold: list[str], governance: dict | None = None) -> type[BaseHTTPRequestHandler]:
    decisions: dict[str, str] = {}
    state = governance_state() if governance is None else governance

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

        def policy_api(self, method: str) -> None:
            url = urlparse(self.path)
            parts = url.path[len(API):].strip("/").split("/")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length)) if length else None
            except ValueError:
                return self.reply(400, {"code": "EINVAL", "message": "the body is not JSON"})
            state["calls"].append((method, url.path, self.headers.get("Authorization", ""), body))
            if not self.authorized():
                return
            if state["fail"].get(method) == 0:
                # 0 drops the connection: the caller gets no answer.
                self.close_connection = True
                return None
            if method in state["fail"]:
                return self.reply(state["fail"][method], {"code": "EMOCK", "message": f"mock: {method} set to fail"})
            if (method, parts) == ("GET", ["agent"]):
                return self.reply(200, {"meta": {"page": {"next": None}}, "data": state["agents"]})
            if (method, parts) == ("GET", ["policy"]):
                query = parse_qs(url.query)
                limit = int(query.get("limit", ["10"])[0])
                after = query.get("after", [""])[0]
                ids = [policy["policy_id"] for policy in state["policies"]]
                start = ids.index(after) + 1 if after in ids else 0
                page = state["policies"][start : start + limit]
                more = start + limit < len(ids)
                link = f"{API}/policy?limit={limit}&after={page[-1]['policy_id']}" if more else None
                return self.reply(200, {"meta": {"page": {"next": link}}, "data": page})
            policy = next((item for item in state["policies"] if parts[0] == "policy" and parts[1:2] == [item["policy_id"]]), None)
            if policy is None:
                return self.reply(404, {"code": "ENOENT", "message": "policy or draft not found"})
            draft = state["drafts"].get(parts[3]) if len(parts) == 4 and parts[2] == "draft" else None
            if (method, parts[2:]) == ("POST", ["draft"]):
                held = [item for item in state["drafts"].values() if item["policy_id"] == policy["policy_id"]]
                if held:
                    # Like Mezmo: the caller resumes the open draft, 200 rather than 201.
                    return self.reply(200, {"data": held[0]})
                fields = ("name", "description", "default_action", "rules")
                draft = {"draft_id": object_id(), "policy_id": policy["policy_id"], **json.loads(json.dumps({key: policy[key] for key in fields}))}
                state["drafts"][draft["draft_id"]] = draft
                policy["draft"] = draft["draft_id"]
                return self.reply(201, {"data": draft})
            if method == "PUT" and draft is not None:
                problem = draft_problem(body, draft)
                if problem:
                    return self.reply(400, {"code": "EINVAL", "message": problem})
                # Like Mezmo: a write without a description clears it.
                draft.update(
                    name=body["name"], description=body.get("description"), default_action=body["default_action"],
                    rules=[compiled(rule) for rule in body.get("rules", [])],
                )
                return self.reply(200, {"data": draft})
            if method == "PATCH" and len(parts) == 2:
                draft_id = (body or {}).get("draft_id")
                if not draft_id or draft_id != policy["draft"]:
                    return self.reply(409, {"code": "ECONFLICT", "message": "draft_id is not this policy's draft"})
                draft = state["drafts"].pop(draft_id)
                policy.update({key: draft[key] for key in ("name", "description", "default_action", "rules")}, draft=None)
                return self.reply(200, {"data": policy})
            if method == "DELETE" and draft is not None:
                del state["drafts"][parts[3]]
                policy["draft"] = None
                self.send_response(204)
                self.end_headers()
                return None
            return self.reply(404, {"code": "ENOENT", "message": "policy or draft not found"})

        def do_PUT(self) -> None:
            self.policy_api("PUT")

        def do_PATCH(self) -> None:
            self.policy_api("PATCH")

        def do_DELETE(self) -> None:
            self.policy_api("DELETE")

        def do_POST(self) -> None:
            if urlparse(self.path).path.startswith(API + "/"):
                return self.policy_api("POST")
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
            if url.path.startswith(API + "/"):
                return self.policy_api("GET")
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
