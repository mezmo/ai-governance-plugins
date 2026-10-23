import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import mezmo_check as check  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
CONFIRMED = '{"status":"ok","message":"Connection confirmed, no records processed."}'


class FakeTransport:
    """Records requests and answers from a table of (url-suffix, is-empty-body) -> (status, body)."""

    def __init__(self, check_status=207, record_status=200, error=None):
        self.calls = []
        self.check_status = check_status
        self.record_status = record_status
        self.error = error

    def __call__(self, method, url, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.error:
            raise self.error
        if json.loads(body) == {}:
            return self.check_status, CONFIRMED if self.check_status == 207 else "denied"
        return self.record_status, "" if self.record_status == 200 else "too big"


class PayloadTest(unittest.TestCase):
    def test_log_payload(self):
        payload = check.test_payload("logs", now_ns=1_700_000_000_000_000_000, run_id="r1")
        resource = payload["resourceLogs"][0]["resource"]
        self.assertIn({"key": "service.name", "value": {"stringValue": "mezmo-claude-plugin"}}, resource["attributes"])
        self.assertIn({"key": "mezmo.test", "value": {"boolValue": True}}, resource["attributes"])
        record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(record["timeUnixNano"], "1700000000000000000")
        self.assertIn({"key": "test.run_id", "value": {"stringValue": "r1"}}, record["attributes"])

    def test_span_payload_uses_hex_ids(self):
        span = check.test_payload("traces", now_ns=10**18, run_id="r1")["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertRegex(span["traceId"], r"^[0-9a-f]{32}$")
        self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
        self.assertEqual(span["name"], "mezmo.test")
        self.assertLess(int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"]))

    def test_metric_payload(self):
        metric = check.test_payload("metrics", now_ns=10**18, run_id="r1")["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        self.assertEqual(metric["name"], "mezmo.test")
        self.assertEqual(metric["gauge"]["dataPoints"][0]["asInt"], "1")


class RunTest(unittest.TestCase):
    def test_checks_and_records_for_every_signal(self):
        transport = FakeTransport()
        results = check.run(KEY, send=transport)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(len(transport.calls), 6)
        first = transport.calls[0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["url"], "https://logs.mezmo.com/otel/v1/traces")
        self.assertEqual(first["headers"]["apikey"], KEY)
        self.assertEqual(first["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(first["body"]), {})

    def test_check_only_sends_no_records(self):
        transport = FakeTransport()
        results = check.run(KEY, check_only=True, send=transport)
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(all(r.step == "connection" for r in results))

    def test_bad_key_stops_before_sending_records(self):
        transport = FakeTransport(check_status=403)
        results = check.run(KEY, send=transport)
        self.assertEqual(len(transport.calls), 3)
        self.assertFalse(results[0].ok)
        self.assertIn("ingestion key", results[0].detail)

    def test_oversize_record_is_explained(self):
        results = check.run(KEY, send=FakeTransport(record_status=413))
        failed = [r for r in results if not r.ok]
        self.assertEqual(len(failed), 3)
        self.assertIn("maximum message size", failed[0].detail)

    def test_network_error_is_reported(self):
        results = check.run(KEY, send=FakeTransport(error=OSError("Name or service not known")))
        self.assertFalse(results[0].ok)
        self.assertIsNone(results[0].status)
        self.assertIn("Name or service not known", results[0].detail)


class CliTest(unittest.TestCase):
    def run_cli(self, *args, environ=None, transport=None):
        out, err = io.StringIO(), io.StringIO()
        transport = transport or FakeTransport()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = check.main(["--all-signals", *args], environ=environ or {}, send=transport)
        return code, out.getvalue(), err.getvalue()

    def test_success_with_key_from_environment(self):
        code, out, err = self.run_cli(environ={"MEZMO_INGESTION_KEY": KEY})
        self.assertEqual(code, 0, err)
        self.assertIn("All checks passed", out)
        self.assertNotIn(KEY, out + err)

    def test_reads_key_from_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "settings.json")
            path.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_HEADERS": f"apikey={KEY}"}}))
            transport = FakeTransport()
            code, _, _ = self.run_cli("--settings-file", str(path), transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.calls[0]["headers"]["apikey"], KEY)

    def test_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = self.run_cli("--settings-file", str(pathlib.Path(tmp, "none.json")))
        self.assertEqual(code, 2)
        self.assertIn("MEZMO_INGESTION_KEY", err)

    def test_failure_exit_code(self):
        code, out, _ = self.run_cli("--key", KEY, transport=FakeTransport(check_status=403))
        self.assertEqual(code, 1)
        self.assertIn("403", out)

    def test_json_output(self):
        code, out, _ = self.run_cli("--key", KEY, "--json", "--check-only")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["key"], "0123…cdef")
        self.assertEqual(len(report["results"]), 3)


class SelectedSignalsTest(unittest.TestCase):
    def run_selected(self, env, args=(), managed=False, environ=None):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "settings.json")
            path.write_text(json.dumps({"env": env}))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    patch.object(check.cco, "managed_settings_with_otel", return_value=["managed"] if managed else []):
                code = check.main(["--settings-file", str(path), *args], environ=environ or {}, send=transport)
        return code, transport.calls, out.getvalue(), err.getvalue()

    def test_configured_traces_only_and_check_only(self):
        env = check.cco.build_env(check.cco.Options(key=KEY, metrics=False, logs=False))
        for args, count in [((), 2), (("--check-only",), 1)]:
            code, calls, out, err = self.run_selected(env, args)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(calls), count)
            self.assertTrue(all(c["url"].endswith("/traces") for c in calls))
            if count == 1:
                self.assertEqual(json.loads(calls[0]["body"]), {})
            else:
                self.assertIn("Destination arrival is not verified", out)

    def test_startup_environment_can_disable_a_saved_signal(self):
        env = check.cco.build_env(check.cco.Options(key=KEY))
        code, calls, _, err = self.run_selected(env, environ={"OTEL_LOGS_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none"})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["url"].endswith("/traces") for c in calls))

    def test_disabled_or_ambiguous_selection_never_sends(self):
        env = check.cco.build_env(check.cco.Options(key=KEY))
        for overrides, managed in [({"CLAUDE_CODE_ENABLE_TELEMETRY": "0"}, False),
                                   ({"OTEL_TRACES_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none"}, False),
                                   ({}, True)]:
            code, calls, _, _ = self.run_selected({**env, **overrides}, managed=managed)
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])

    def test_explicit_signals_and_all_signals(self):
        env = {"OTEL_EXPORTER_OTLP_HEADERS": f"apikey={KEY}"}
        for args, names in [(("--signals", "traces", "logs"), {"traces", "logs"}),
                             (("--all-signals",), {"traces", "logs", "metrics"})]:
            code, calls, _, err = self.run_selected(env, args)
            self.assertEqual(code, 0, err)
            self.assertEqual({c["url"].rsplit("/", 1)[-1] for c in calls}, names)
            self.assertEqual(len(calls), 2 * len(names))


if __name__ == "__main__":
    unittest.main()
