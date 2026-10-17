import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import claude_code_otel as cco  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"
ENDPOINT = "https://logs.mezmo.com/otel"


def opts(**kwargs):
    kwargs.setdefault("key", KEY)
    return cco.Options(**kwargs)


class BuildEnvTest(unittest.TestCase):
    def test_all_signals_by_default(self):
        self.assertEqual(
            cco.build_env(opts()),
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_TRACES_EXPORTER": "otlp",
                "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
                "OTEL_EXPORTER_OTLP_ENDPOINT": ENDPOINT,
                "OTEL_EXPORTER_OTLP_HEADERS": f"apikey={KEY}",
            },
        )

    def test_metrics_only(self):
        env = cco.build_env(opts(logs=False, traces=False))
        self.assertEqual(env["OTEL_METRICS_EXPORTER"], "otlp")
        for absent in ("OTEL_LOGS_EXPORTER", "OTEL_TRACES_EXPORTER", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"):
            self.assertNotIn(absent, env)

    def test_requires_at_least_one_signal(self):
        with self.assertRaises(ValueError):
            cco.build_env(opts(metrics=False, logs=False, traces=False))

    def test_prompt_logging_keeps_responses_redacted_unless_asked(self):
        env = cco.build_env(opts(log_prompts=True))
        self.assertEqual(env["OTEL_LOG_USER_PROMPTS"], "1")
        self.assertEqual(env["OTEL_LOG_ASSISTANT_RESPONSES"], "0")

    def test_response_and_tool_logging(self):
        env = cco.build_env(opts(log_responses=True, log_tool_details=True))
        self.assertEqual(env["OTEL_LOG_ASSISTANT_RESPONSES"], "1")
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1")
        self.assertNotIn("OTEL_LOG_USER_PROMPTS", env)

    def test_rejects_bad_key(self):
        with self.assertRaises(ValueError):
            cco.build_env(opts(key="has,comma"))


class ConfigureTest(unittest.TestCase):
    def test_empty_settings(self):
        result = cco.apply_configure({}, opts())
        self.assertEqual(result["env"], cco.build_env(opts()))

    def test_preserves_unrelated_settings_and_env(self):
        settings = {"model": "opus", "env": {"FOO": "bar"}, "permissions": {"allow": ["Bash(ls)"]}}
        result = cco.apply_configure(settings, opts())
        self.assertEqual(result["model"], "opus")
        self.assertEqual(result["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(result["env"]["FOO"], "bar")
        self.assertEqual(result["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], ENDPOINT)

    def test_does_not_mutate_input(self):
        settings = {"env": {"FOO": "bar"}}
        cco.apply_configure(settings, opts())
        self.assertEqual(settings, {"env": {"FOO": "bar"}})

    def test_adds_otlp_to_existing_non_otlp_exporters(self):
        settings = {"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1", "OTEL_METRICS_EXPORTER": "console"}}
        result = cco.apply_configure(settings, opts())
        self.assertEqual(result["env"]["OTEL_METRICS_EXPORTER"], "console,otlp")

    def test_replaces_none_exporter(self):
        settings = {"env": {"OTEL_TRACES_EXPORTER": "none"}}
        result = cco.apply_configure(settings, opts())
        self.assertEqual(result["env"]["OTEL_TRACES_EXPORTER"], "otlp")

    def test_refuses_to_replace_another_otlp_endpoint(self):
        settings = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com", "OTEL_LOGS_EXPORTER": "otlp"}}
        with self.assertRaises(cco.ConflictError) as ctx:
            cco.apply_configure(settings, opts())
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", ctx.exception.keys)

    def test_existing_otlp_exporter_to_default_endpoint_is_a_conflict(self):
        settings = {"env": {"OTEL_METRICS_EXPORTER": "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}}
        with self.assertRaises(cco.ConflictError) as ctx:
            cco.apply_configure(settings, opts())
        self.assertIn("OTEL_METRICS_EXPORTER", ctx.exception.keys)

    def test_force_replaces_another_endpoint(self):
        settings = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com", "OTEL_EXPORTER_OTLP_HEADERS": "x=y"}}
        result = cco.apply_configure(settings, opts(), force=True)
        self.assertEqual(result["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], ENDPOINT)
        self.assertEqual(result["env"]["OTEL_EXPORTER_OTLP_HEADERS"], f"apikey={KEY}")

    def test_per_signal_overrides_are_conflicts_and_force_removes_them(self):
        settings = {"env": {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://localhost:4318/v1/traces",
                            "OTEL_EXPORTER_OTLP_LOGS_HEADERS": "x=y"}}
        with self.assertRaises(cco.ConflictError) as ctx:
            cco.apply_configure(settings, opts())
        self.assertEqual(
            sorted(ctx.exception.keys),
            ["OTEL_EXPORTER_OTLP_LOGS_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
        )
        result = cco.apply_configure(settings, opts(), force=True)
        self.assertNotIn("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", result["env"])
        self.assertNotIn("OTEL_EXPORTER_OTLP_LOGS_HEADERS", result["env"])

    def test_otel_headers_helper_is_a_conflict(self):
        settings = {"otelHeadersHelper": "/usr/local/bin/get-headers"}
        with self.assertRaises(cco.ConflictError) as ctx:
            cco.apply_configure(settings, opts())
        self.assertIn("otelHeadersHelper", ctx.exception.keys)
        result = cco.apply_configure(settings, opts(), force=True)
        self.assertNotIn("otelHeadersHelper", result)

    def test_reconfigure_turns_signals_and_content_logging_off(self):
        first = cco.apply_configure({}, opts(log_prompts=True, log_tool_details=True))
        second = cco.apply_configure(first, opts(traces=False))
        env = second["env"]
        for absent in ("OTEL_TRACES_EXPORTER", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
                       "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_DETAILS"):
            self.assertNotIn(absent, env)
        self.assertEqual(env["OTEL_LOGS_EXPORTER"], "otlp")

    def test_reconfigure_keeps_other_exporters_when_signal_turned_off(self):
        first = cco.apply_configure({"env": {"OTEL_TRACES_EXPORTER": "console"}}, opts())
        self.assertEqual(first["env"]["OTEL_TRACES_EXPORTER"], "console,otlp")
        second = cco.apply_configure(first, opts(traces=False))
        self.assertEqual(second["env"]["OTEL_TRACES_EXPORTER"], "console")

    def test_resource_attributes_merge_with_existing(self):
        settings = {"env": {"OTEL_RESOURCE_ATTRIBUTES": "team=a,dept=b"}}
        result = cco.apply_configure(settings, opts(resource_attributes="team=c,cost_center=eng-1"))
        self.assertEqual(result["env"]["OTEL_RESOURCE_ATTRIBUTES"], "team=c,dept=b,cost_center=eng-1")

    def test_resource_attributes_reject_unencoded_spaces(self):
        with self.assertRaises(ValueError):
            cco.apply_configure({}, opts(resource_attributes="org=Acme Corp"))


class RemoveTest(unittest.TestCase):
    def test_removes_what_configure_added(self):
        original = {"model": "opus", "env": {"FOO": "bar"}}
        configured = cco.apply_configure(original, opts(log_prompts=True))
        removed, keys = cco.apply_remove(configured)
        self.assertEqual(removed, original)
        self.assertIn("OTEL_EXPORTER_OTLP_HEADERS", keys)

    def test_drops_empty_env(self):
        removed, _ = cco.apply_remove(cco.apply_configure({}, opts()))
        self.assertEqual(removed, {})

    def test_keeps_other_exporters_and_telemetry_switch(self):
        configured = cco.apply_configure({"env": {"OTEL_METRICS_EXPORTER": "console"}}, opts())
        removed, _ = cco.apply_remove(configured)
        self.assertEqual(removed["env"]["OTEL_METRICS_EXPORTER"], "console")
        self.assertEqual(removed["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")

    def test_leaves_resource_attributes(self):
        configured = cco.apply_configure({}, opts(resource_attributes="team=a"))
        removed, _ = cco.apply_remove(configured)
        self.assertEqual(removed["env"], {"OTEL_RESOURCE_ATTRIBUTES": "team=a"})

    def test_refuses_when_not_configured_for_mezmo(self):
        settings = {"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example.com"}}
        with self.assertRaises(cco.NotConfiguredError):
            cco.apply_remove(settings)
        removed, _ = cco.apply_remove(settings, force=True)
        self.assertEqual(removed, {})


class FileIOTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.path = self.dir / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(cco.read_settings(self.path), {})

    def test_invalid_json_is_an_error(self):
        self.path.write_text("{ not json")
        with self.assertRaises(cco.SettingsError):
            cco.read_settings(self.path)

    def test_non_object_is_an_error(self):
        self.path.write_text("[]")
        with self.assertRaises(cco.SettingsError):
            cco.read_settings(self.path)

    def test_new_file_is_private(self):
        backup = cco.write_settings(self.path, {"a": 1})
        self.assertIsNone(backup)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.path.read_text(), '{\n  "a": 1\n}\n')

    def test_existing_file_is_backed_up_and_mode_kept(self):
        self.path.write_text('{"old": true}')
        os.chmod(self.path, 0o640)
        backup = cco.write_settings(self.path, {"new": True})
        self.assertIsNotNone(backup)
        self.assertEqual(json.loads(backup.read_text()), {"old": True})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(json.loads(self.path.read_text()), {"new": True})

    def test_symlinked_settings_stay_a_symlink(self):
        real = self.dir / "dotfiles-settings.json"
        real.write_text("{}")
        self.path.symlink_to(real)
        cco.write_settings(self.path, {"a": 1})
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(json.loads(real.read_text()), {"a": 1})

    def test_creates_parent_directory(self):
        nested = self.dir / "cfg" / "settings.json"
        cco.write_settings(nested, {})
        self.assertTrue(nested.exists())


class ManagedAndStatusTest(unittest.TestCase):
    def test_managed_document_is_env_block(self):
        self.assertEqual(cco.managed_document(opts()), {"env": cco.build_env(opts())})

    def test_status_of_configured_settings(self):
        report = cco.status(cco.apply_configure({}, opts(traces=False)))
        self.assertTrue(report["configured"])
        self.assertEqual(report["key"], "0123…cdef")
        self.assertEqual(report["signals"], {"metrics": True, "logs": True, "traces": False})
        self.assertEqual(report["conflicts"], [])

    def test_status_of_empty_settings(self):
        report = cco.status({})
        self.assertFalse(report["configured"])
        self.assertEqual(report["key"], "(not set)")


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name, "settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cco.main(["--settings-file", str(self.path), *args], environ=environ or {})
        return code, out.getvalue(), err.getvalue()

    def test_configure_writes_file_and_never_prints_the_key(self):
        code, out, err = self.run_cli("configure", "--no-traces", environ={"MEZMO_INGESTION_KEY": KEY})
        self.assertEqual(code, 0, err)
        env = json.loads(self.path.read_text())["env"]
        self.assertEqual(env["OTEL_EXPORTER_OTLP_HEADERS"], f"apikey={KEY}")
        self.assertNotIn("OTEL_TRACES_EXPORTER", env)
        self.assertNotIn(KEY, out + err)
        self.assertIn("0123…cdef", out)

    def test_reconfigure_reuses_the_configured_key(self):
        self.run_cli("configure", "--key", KEY)
        code, _, err = self.run_cli("configure", "--no-traces")
        self.assertEqual(code, 0, err)
        env = json.loads(self.path.read_text())["env"]
        self.assertEqual(env["OTEL_EXPORTER_OTLP_HEADERS"], f"apikey={KEY}")
        self.assertNotIn("OTEL_TRACES_EXPORTER", env)

    def test_configure_without_key_fails(self):
        code, _, err = self.run_cli("configure")
        self.assertEqual(code, 2)
        self.assertIn("MEZMO_INGESTION_KEY", err)

    def test_configure_conflict_exit_code(self):
        self.path.write_text(json.dumps({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://x.example.com"}}))
        code, _, err = self.run_cli("configure", "--key", KEY)
        self.assertEqual(code, 3)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", err)
        self.assertIn("--force", err)

    def test_remove_round_trip(self):
        self.run_cli("configure", "--key", KEY)
        code, out, _ = self.run_cli("remove")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.path.read_text()), {})
        self.assertIn("Removed", out)

    def test_status_json(self):
        self.run_cli("configure", "--key", KEY)
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["configured"])
        self.assertNotIn(KEY, out)

    def test_managed_prints_document_without_touching_file(self):
        code, out, _ = self.run_cli("managed", "--key", KEY, "--no-logs")
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(out)["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"], ENDPOINT)

    def test_managed_merges_into_a_document_governance_wrote(self):
        doc = pathlib.Path(self._tmp.name, "managed.json")
        doc.write_text(json.dumps({
            "enabledPlugins": {"mezmo@mezmo": True},
            "env": {"MEZMO_GOVERNANCE_ACCESS_KEY": "sts_x", "OTEL_TRACES_EXPORTER": "otlp",
                    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1"},
        }))
        code, out, err = self.run_cli("managed", "--key", KEY, "--no-traces", "--output", str(doc))
        self.assertEqual(code, 0, err)
        document = json.loads(doc.read_text())
        self.assertEqual(document["enabledPlugins"], {"mezmo@mezmo": True})
        env = document["env"]
        self.assertEqual(env["MEZMO_GOVERNANCE_ACCESS_KEY"], "sts_x")
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], ENDPOINT)
        # A rerun replaces this script's own keys, so a signal turned off is gone.
        self.assertNotIn("OTEL_TRACES_EXPORTER", env)
        self.assertNotIn("CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", env)
        self.assertNotIn(KEY, out + err)


if __name__ == "__main__":
    unittest.main()
