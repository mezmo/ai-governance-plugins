import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import mezmo_common as mc  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"


class MaskTest(unittest.TestCase):
    def test_masks_middle_of_key(self):
        self.assertEqual(mc.mask(KEY), "0123…cdef")

    def test_short_values_are_fully_masked(self):
        self.assertEqual(mc.mask("abc"), "****")

    def test_none_is_shown_as_unset(self):
        self.assertEqual(mc.mask(None), "(not set)")


class ValidateKeyTest(unittest.TestCase):
    def test_strips_surrounding_whitespace(self):
        self.assertEqual(mc.validate_ingestion_key(f"  {KEY}\n"), KEY)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            mc.validate_ingestion_key("   ")

    def test_rejects_characters_that_break_the_header_list(self):
        for bad in ("abc,def", "abc=def", "abc def", "abc;def", "abcé"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mc.validate_ingestion_key(bad)


class OtlpHeadersTest(unittest.TestCase):
    def test_parses_comma_separated_pairs(self):
        self.assertEqual(
            mc.parse_otlp_headers(f"apikey={KEY}, x-team = core"),
            {"apikey": KEY, "x-team": "core"},
        )

    def test_percent_decodes_values(self):
        self.assertEqual(mc.parse_otlp_headers("a=b%20c"), {"a": "b c"})

    def test_ignores_empty_and_malformed_entries(self):
        self.assertEqual(mc.parse_otlp_headers(",novalue,a=1,"), {"a": "1"})

    def test_formats_pairs(self):
        self.assertEqual(mc.format_otlp_headers({"apikey": KEY}), f"apikey={KEY}")


class EndpointTest(unittest.TestCase):
    def test_signal_url(self):
        self.assertEqual(mc.signal_url("traces"), "https://logs.mezmo.com/otel/v1/traces")

    def test_is_mezmo_endpoint_tolerates_trailing_slash_and_case(self):
        self.assertTrue(mc.is_mezmo_endpoint("https://logs.mezmo.com/otel/"))
        self.assertTrue(mc.is_mezmo_endpoint("HTTPS://LOGS.MEZMO.COM/otel"))
        self.assertFalse(mc.is_mezmo_endpoint("https://otlp.example.com"))
        self.assertFalse(mc.is_mezmo_endpoint(None))


class SettingsPathTest(unittest.TestCase):
    def test_uses_claude_config_dir_when_set(self):
        path = mc.default_settings_path({"CLAUDE_CONFIG_DIR": "/tmp/cfg", "HOME": "/home/u"})
        self.assertEqual(path, pathlib.Path("/tmp/cfg/settings.json"))

    def test_defaults_to_home_dot_claude(self):
        path = mc.default_settings_path({"HOME": "/home/u"})
        self.assertEqual(path, pathlib.Path("/home/u/.claude/settings.json"))


class KeyFromSettingsTest(unittest.TestCase):
    def test_reads_apikey_from_configured_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "settings.json")
            path.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_HEADERS": f"apikey={KEY}"}}))
            self.assertEqual(mc.key_from_settings(path), KEY)

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mc.key_from_settings(pathlib.Path(tmp, "nope.json")))


class ResolveKeyTest(unittest.TestCase):
    def test_explicit_key_wins(self):
        self.assertEqual(mc.resolve_key(KEY, {"MEZMO_INGESTION_KEY": "other"}), KEY)

    def test_falls_back_to_environment(self):
        self.assertEqual(mc.resolve_key(None, {"MEZMO_INGESTION_KEY": KEY}), KEY)

    def test_returns_none_when_nothing_given(self):
        self.assertIsNone(mc.resolve_key(None, {}))


class HttpRequestTest(unittest.TestCase):
    def test_a_redirect_is_returned_and_the_key_never_goes_elsewhere(self):
        seen = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        class Redirect(Elsewhere):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Elsewhere, Redirect)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        target = f"http://127.0.0.1:{servers[0].server_port}/elsewhere"
        url = f"http://127.0.0.1:{servers[1].server_port}/check"

        status, _ = mc.http_request("GET", url, {"Authorization": "Token secret"})

        self.assertEqual(seen, [])
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()
