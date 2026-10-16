#!/usr/bin/env python3
"""Check that Mezmo's OpenTelemetry endpoint accepts your ingestion key.

For traces, logs and metrics it first sends Mezmo's connection check (an empty JSON
object, answered with HTTP 207 and not published), then one small OTLP/JSON test record
with service.name=mezmo-claude-plugin and mezmo.test=true (answered with HTTP 200).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import secrets
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import mezmo_common as mc

SERVICE_NAME = "mezmo-claude-plugin"
Transport = Callable[..., Any]


@dataclasses.dataclass
class Result:
    signal: str
    step: str  # "connection" or "record"
    status: Optional[int]
    ok: bool
    detail: str


def _attr(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def test_payload(signal: str, now_ns: int, run_id: str) -> Dict[str, Any]:
    """One OTLP/JSON record for the signal, tagged so it is easy to find and filter out."""
    resource = {"attributes": [_attr("service.name", SERVICE_NAME), _attr("mezmo.test", True)]}
    scope = {"name": SERVICE_NAME}
    attributes = [_attr("test.run_id", run_id)]
    ts = str(now_ns)
    if signal == "logs":
        record = {
            "timeUnixNano": ts,
            "observedTimeUnixNano": ts,
            "severityNumber": 9,
            "severityText": "INFO",
            "body": {"stringValue": "Mezmo Claude plugin test record"},
            "attributes": attributes,
        }
        return {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": scope, "logRecords": [record]}]}]}
    if signal == "traces":
        span = {
            "traceId": secrets.token_hex(16),
            "spanId": secrets.token_hex(8),
            "name": "mezmo.test",
            "kind": 1,
            "startTimeUnixNano": str(now_ns - 1_000_000),
            "endTimeUnixNano": ts,
            "attributes": attributes,
            "status": {"code": 1},
        }
        return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": scope, "spans": [span]}]}]}
    if signal == "metrics":
        metric = {
            "name": "mezmo.test",
            "description": "Mezmo Claude plugin test metric",
            "unit": "1",
            "gauge": {"dataPoints": [{"asInt": "1", "timeUnixNano": ts, "attributes": attributes}]},
        }
        return {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": scope, "metrics": [metric]}]}]}
    raise ValueError(f"Unknown signal {signal!r}")


def explain(status: int, body: str) -> str:
    if status == 207 and "Connection confirmed" in body:
        return "connection confirmed"
    if 200 <= status < 300:
        return "accepted"
    if status in (401, 403):
        return "Mezmo rejected the ingestion key. Check it under Settings > API Keys in Mezmo."
    if status == 413:
        return "The record is larger than Mezmo's maximum message size."
    if status == 404:
        return f"Not found. The endpoint should be {mc.MEZMO_OTEL_ENDPOINT}/v1/<signal>."
    if status == 400:
        return f"Mezmo rejected the payload: {body.strip()[:200]}"
    if status >= 500:
        return "Mezmo returned a server error. Try again in a few minutes."
    return f"Unexpected response: {body.strip()[:200]}"


def _post(signal: str, key: str, payload: Mapping[str, Any], step: str, send: Transport) -> Result:
    headers = {mc.MEZMO_AUTH_HEADER: key, "Content-Type": "application/json"}
    try:
        status, body = send("POST", mc.signal_url(signal), headers, json.dumps(payload).encode())
    except OSError as err:
        return Result(signal, step, None, False, f"Network error: {err}")
    expected = 207 if step == "connection" else 200
    ok = status == expected or (step == "record" and 200 <= status < 300)
    return Result(signal, step, status, ok, explain(status, body))


def run(key: str, check_only: bool = False, send: Transport = mc.http_request, run_id: Optional[str] = None) -> List[Result]:
    run_id = run_id or secrets.token_hex(4)
    results = [_post(signal, key, {}, "connection", send) for signal in mc.SIGNALS]
    if check_only or not all(r.ok for r in results):
        return results
    now_ns = time.time_ns()
    for signal in mc.SIGNALS:
        results.append(_post(signal, key, test_payload(signal, now_ns, run_id), "record", send))
    return results


def main(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    send: Transport = mc.http_request,
) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="mezmo_check.py", description=__doc__.splitlines()[0])
    parser.add_argument("--key", help=f"Mezmo ingestion key (default: ${mc.KEY_ENV_VAR}, then the configured settings)")
    parser.add_argument("--settings-file", type=pathlib.Path, help="Claude Code settings file to read the key from")
    parser.add_argument("--check-only", action="store_true", help="Only run the connection check; publish nothing")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args(argv)

    key = mc.resolve_key(args.key, environ) or mc.key_from_settings(
        args.settings_file or mc.default_settings_path(environ)
    )
    if not key:
        print(f"No ingestion key. Pass --key, set {mc.KEY_ENV_VAR}, or run /mezmo:setup first.", file=sys.stderr)
        return 2
    try:
        key = mc.validate_ingestion_key(key)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2

    run_id = secrets.token_hex(4)
    results = run(key, check_only=args.check_only, send=send, run_id=run_id)
    passed = all(r.ok for r in results)

    if args.json:
        print(json.dumps({
            "endpoint": mc.MEZMO_OTEL_ENDPOINT,
            "key": mc.mask(key),
            "run_id": run_id,
            "passed": passed,
            "results": [dataclasses.asdict(r) for r in results],
        }, indent=2))
        return 0 if passed else 1

    print(f"Mezmo endpoint {mc.MEZMO_OTEL_ENDPOINT}, ingestion key {mc.mask(key)}")
    for r in results:
        label = "connection check" if r.step == "connection" else "test record"
        status = r.status if r.status is not None else "---"
        print(f"  {'ok  ' if r.ok else 'FAIL'}  {r.signal:<8} {label:<16} {status}  {r.detail}")
    if not passed:
        print("Some checks failed.")
        return 1
    if args.check_only:
        print("All checks passed. Nothing was published.")
    else:
        print(f"All checks passed. In Mezmo, look for service.name={SERVICE_NAME} and test.run_id={run_id}.")
        print("Data shows up once a pipeline has an OpenTelemetry source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
