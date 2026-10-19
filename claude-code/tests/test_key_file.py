"""Private token handoff: synthetic values only, no real Claude or HTTP calls."""
import contextlib
import io
import json
import os
import pathlib
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import claude_code_otel as otel
import governance_config as gc
import governance_hook as hook
import mcp_config as mcp
import mezmo_check as check
import mezmo_common as mc
from test_governance_config import FakeClaude, FakeHttp
from test_mcp_config import FakeClaude as FakeMcp, FakeHttp as FakeMcpHttp
from test_mezmo_check import FakeTransport

KEY = "synthetic-only-token-0123456789"


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "key"
        self.settings = self.root / "settings.json"
        self.write_key()

    def write_key(self, content=KEY):
        self.path.write_text(content)
        self.path.chmod(0o600)

    def capture(self, call):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = call()
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def args(self):
        return ["--key-file", str(self.path), "--delete-key-file"]

    def test_create_and_delete_helper(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mc.key_file_main(["create-key-file"]), 0)
        path = pathlib.Path(out.getvalue().strip())
        try:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.read_bytes(), b"")
            self.assertEqual(mc.key_file_main(["delete-key-file", str(path)]), 0)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        finally:
            if path.exists():
                mc.delete_key_file(path)

    def test_read_keeps_file_unless_cleanup_requested(self):
        self.write_key(KEY + "\n")
        self.assertEqual(mc.read_key_file(self.path), KEY)
        self.assertTrue(self.path.exists())

    def test_rejects_shared_file_and_directory(self):
        for target, mode in [(self.path, 0o644), (self.root, 0o755)]:
            with self.subTest(target=target):
                original = target.stat().st_mode & 0o777
                target.chmod(mode)
                try:
                    with self.assertRaises(ValueError):
                        mc.read_key_file(self.path)
                finally:
                    target.chmod(original)

    def test_rejects_wrong_owner(self):
        with patch.object(mc.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                mc.read_key_file(self.path)

    def test_rejects_symlinks_hardlinks_and_nonregular_files(self):
        link = self.root / "link"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            mc.read_key_file(link)
        with self.assertRaises((OSError, ValueError)):
            mc.delete_key_file(link)
        self.assertEqual(self.path.read_text(), KEY)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            mc.read_key_file(link)
        link.unlink()
        link.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            mc.read_key_file(link)

    def test_rejects_parent_symlink(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            mc.read_key_file(alias / "key")

    def test_rejects_empty_multiline_large_and_invalid_utf8_without_echo(self):
        for raw in [b"", (KEY + "\nother").encode(), b"x" * 16385, b"\xff"]:
            self.path.write_bytes(raw)
            with self.assertRaises(ValueError) as caught:
                mc.read_key_file(self.path)
            self.assertNotIn(KEY, str(caught.exception))

    def test_governance_file_success_and_rejected_key_cleanup(self):
        for status, expected in [(404, 0), (401, 3)]:
            self.write_key()
            http, claude = FakeHttp(status), FakeClaude()
            code, _, err = self.capture(lambda: gc.main(
                ["--settings-file", str(self.settings), "configure", *self.args()],
                environ={}, send=http, claude=claude, managed_files=[]))
            self.assertEqual(code, expected, err)
            self.assertFalse(self.path.exists())
            self.assertTrue(http.calls)
            self.assertFalse(any(KEY in " ".join(c["args"]) for c in claude.calls))

    def test_old_runtime_cleans_file_without_reading_or_sending_token(self):
        http = FakeHttp()
        with patch.object(mc, "read_key_file", side_effect=AssertionError("read token")):
            code, _, _ = self.capture(lambda: gc.main(
                ["configure", *self.args()], environ={}, send=http,
                claude=lambda args, stdin: (0, "2.1.212"), managed_files=[]))
        self.assertEqual(code, 5)
        self.assertEqual(http.calls, [])
        self.assertFalse(self.path.exists())

    def test_telemetry_and_endpoint_check_file_cleanup(self):
        code, _, err = self.capture(lambda: otel.main(
            ["--settings-file", str(self.settings), "configure", *self.args(), "--no-logs", "--no-metrics"], environ={}))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.path.exists())
        self.write_key()
        transport = FakeTransport()
        code, _, err = self.capture(lambda: check.main(
            [*self.args(), "--signals", "traces", "--check-only"], environ={}, send=transport))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(json.loads(transport.calls[0]["body"]), {})

    def test_mcp_file_add_and_check(self):
        for command in ["add", "check"]:
            self.write_key()
            code, _, err = self.capture(lambda: mcp.main(
                [command, *self.args()], environ={}, send=FakeMcpHttp(), claude=FakeMcp()))
            self.assertEqual(code, 0, err)
            self.assertFalse(self.path.exists())

    def test_managed_file_requires_output_and_cleans_on_failure(self):
        for module in [gc, otel]:
            self.write_key()
            code, out, err = self.capture(lambda: module.main(["managed", *self.args()], environ={}))
            self.assertNotEqual(code, 0)
            self.assertIn("--output", err)
            self.assertFalse(self.path.exists())

    def test_managed_output_file_and_cleanup(self):
        for module in [gc, otel]:
            self.write_key()
            target = self.root / (module.__name__ + ".json")
            code, _, err = self.capture(lambda: module.main(
                ["managed", *self.args(), "--output", str(target)], environ={}))
            self.assertEqual(code, 0, err)
            self.assertFalse(self.path.exists())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_keyfile_governed_payload_contains_no_token(self):
        command = shlex.join(["python3", "governance_config.py", "configure", *self.args()])
        payload = hook.bash_items({"command": command})
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertIn(str(self.path), json.dumps(payload))

    def test_cleanup_warning_does_not_expose_contents(self):
        self.path.chmod(0o644)
        code, out, err = self.capture(lambda: check.main(
            [*self.args(), "--signals", "traces"], environ={}, send=FakeTransport()))
        self.assertEqual(code, 2)
        self.assertTrue(self.path.exists())
        self.assertIn("cleanup failed", err)

    def test_cli_error_and_network_failure_clean_private_files(self):
        for module, command, transport, claude in [
            (gc, "configure", FakeHttp(), FakeClaude(refuse="keychain unavailable")),
            (mcp, "check", FakeMcpHttp(error=OSError("offline")), FakeMcp()),
        ]:
            self.write_key()
            kwargs = {"environ": {}, "send": transport, "claude": claude}
            if module is gc:
                kwargs["managed_files"] = []
            code, _, _ = self.capture(lambda: module.main([command, *self.args()], **kwargs))
            self.assertNotEqual(code, 0)
            self.assertFalse(self.path.exists())

    def test_key_and_file_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            gc.main(["configure", "--key", KEY, "--key-file", str(self.path)], environ={})
        # Parsing did not run an action: the caller still owns cleanup.
        self.assertTrue(self.path.exists())
