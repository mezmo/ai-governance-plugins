"""mezmo_setup.py and mezmo_settings.py: where the hooks find the URL and key, and how setup saves them.

No test touches the real Keychain, keyring or ~/.mezmo: the settings folder is a temp folder and every
security / secret-tool command is a stand-in that records its argv and stdin.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mezmo_governance_hook as hook
import mezmo_ledger_hook as ledger
import mezmo_settings
import mezmo_setup

KEY = "canary-key-5d1c8e0a"
API = "https://api.example.test"
ENV_NAMES = ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN", "MEZMO_LEDGER_URL", "MEZMO_LEDGER_KEY")


class FakeStore:
    """Stands in for subprocess.run: answers security and secret-tool like an empty store, and keeps a key given on stdin."""

    def __init__(self, fail_reads: bool = False, fail_deletes: bool = False, hang_secret_tool: bool = False) -> None:
        self.calls: list[tuple[list, str]] = []
        self.key: str | None = None
        self.fail_reads = fail_reads
        self.fail_deletes = fail_deletes
        self.hang_secret_tool = hang_secret_tool

    def __call__(self, args: list, **kwargs: Any) -> subprocess.CompletedProcess:
        stdin = kwargs.get("input") or ""
        self.calls.append((list(args), stdin))
        out, code = "", 0
        if args[:2] == ["security", "-i"]:
            # The test only needs the -w value back; it is the last quoted word.
            self.key = stdin.rsplit('"', 2)[-2]
        elif args[:2] == ["security", "find-generic-password"] or args[:2] == ["secret-tool", "lookup"]:
            if self.fail_reads:
                code = 51
            elif self.key is None:
                code = 44 if args[0] == "security" else 1
            else:
                out = self.key + "\n"
        elif args[:2] == ["secret-tool", "store"]:
            if self.hang_secret_tool:
                raise subprocess.TimeoutExpired(args, kwargs.get("timeout") or 30)
            self.key = stdin
        elif args[:2] in (["security", "delete-generic-password"], ["secret-tool", "clear"]):
            if self.fail_deletes:
                code = 51
            else:
                code, self.key = (0, None) if self.key is not None else (44, None)
        return subprocess.CompletedProcess(args, code, out, "")

    def assert_key_never_in_argv(self, case: unittest.TestCase) -> None:
        for args, _ in self.calls:
            case.assertFalse(any(KEY in arg for arg in args), args)


class SettingsCase(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name) / ".mezmo"
        for patcher in (
            patch.object(mezmo_settings, "settings_dir", lambda: self.dir),
            patch.dict(os.environ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        mezmo_settings._read_store.cache_clear()
        self.addCleanup(mezmo_settings._read_store.cache_clear)
        self.stderr = StringIO()
        stderr = patch.object(sys, "stderr", self.stderr)
        stderr.start()
        self.addCleanup(stderr.stop)

    def save(self, **config: Any) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.dir / "codex.json").write_text(json.dumps(config), encoding="utf-8")

    def save_key_file(self, key: str = KEY, mode: int = 0o600) -> Path:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.dir / "codex-key"
        path.write_text(key + "\n", encoding="utf-8")
        path.chmod(mode)
        return path


class PrecedenceTests(SettingsCase):
    def test_env_then_file_and_store_then_default(self) -> None:
        # Nothing saved: the production URLs and no key.
        self.assertEqual(mezmo_settings.governance_url(), ("https://api.mezmo.com/governance/workflow/authorize", "default"))
        self.assertEqual(mezmo_settings.ledger_url(), ("https://api.mezmo.com/v1/audit/events", "default"))
        self.assertEqual(mezmo_settings.governance_key().value, None)

        # Saved: codex.json's URL and the stored key.
        self.save(api_url=API + "/", key_store="file")
        self.save_key_file()
        self.assertEqual(mezmo_settings.governance_url(), (API + "/governance/workflow/authorize", "codex.json"))
        self.assertEqual(mezmo_settings.ledger_url(), (API + "/v1/audit/events", "codex.json"))
        self.assertEqual(mezmo_settings.governance_key(), (KEY, "file"))
        self.assertEqual(mezmo_settings.ledger_key(), (KEY, "file"))

        # The environment wins over both, value by value.
        os.environ["MEZMO_GOVERNANCE_TOKEN"] = "env-key"
        os.environ["MEZMO_GOVERNANCE_URL"] = "https://other.example.test/governance/workflow/authorize"
        self.assertEqual(mezmo_settings.governance_key(), ("env-key", "env MEZMO_GOVERNANCE_TOKEN"))
        self.assertEqual(mezmo_settings.governance_url().value, "https://other.example.test/governance/workflow/authorize")
        # The ledger follows a governance URL from the environment, never a saved or default host.
        self.assertEqual(mezmo_settings.ledger_url().value, "https://other.example.test/v1/audit/events")
        os.environ["MEZMO_LEDGER_URL"] = "https://ledger.example.test/v1/audit/events"
        os.environ["MEZMO_LEDGER_KEY"] = "ledger-key"
        self.assertEqual(mezmo_settings.ledger_url().value, "https://ledger.example.test/v1/audit/events")
        self.assertEqual(mezmo_settings.ledger_key(), ("ledger-key", "env MEZMO_LEDGER_KEY"))

    def test_the_hooks_read_the_saved_settings(self) -> None:
        self.save(api_url=API, key_store="file")
        self.save_key_file()

        self.assertEqual(hook.authorize_url(), API + "/governance/workflow/authorize")
        self.assertEqual(hook.authorization_header(), f"Token {KEY}")
        config = ledger.load_ledger_config()
        self.assertEqual((config.url, config.key), (API + "/v1/audit/events", KEY))
        self.assertTrue(ledger.governance_configured())

    def test_ledger_enabled_false_turns_off_only_the_derived_ledger_url(self) -> None:
        self.save(api_url=API, ledger_enabled=False, key_store="file")
        self.assertIsNone(mezmo_settings.ledger_url().value)
        os.environ["MEZMO_LEDGER_URL"] = "https://ledger.example.test/v1/audit/events"
        self.assertEqual(mezmo_settings.ledger_url().value, "https://ledger.example.test/v1/audit/events")

    def test_no_key_anywhere_denies_with_the_setup_step(self) -> None:
        with self.assertRaises(hook.HookContractError) as raised:
            hook.authorization_header()
        self.assertIn("setup codex", str(raised.exception))


class StoreReadTests(SettingsCase):
    def test_the_keychain_is_read_once_per_process(self) -> None:
        self.save(key_store="keychain")
        fake = FakeStore()
        fake.key = KEY
        with patch.object(subprocess, "run", fake):
            self.assertEqual(mezmo_settings.governance_key().value, KEY)
            self.assertEqual(mezmo_settings.ledger_key().value, KEY)
        self.assertEqual(
            fake.calls, [(["security", "find-generic-password", "-s", "mezmo-ai-governance", "-a", "codex", "-w"], "")]
        )

    def test_secret_tool_is_read_with_its_lookup(self) -> None:
        self.save(key_store="secret-tool")
        fake = FakeStore()
        fake.key = KEY
        with patch.object(subprocess, "run", fake):
            self.assertEqual(mezmo_settings.governance_key().value, KEY)
        self.assertEqual(fake.calls, [(["secret-tool", "lookup", "service", "mezmo-ai-governance", "account", "codex"], "")])

    def test_a_store_that_fails_warns_and_counts_as_no_key(self) -> None:
        self.save(key_store="keychain")
        with patch.object(subprocess, "run", FakeStore(fail_reads=True)):
            self.assertIsNone(mezmo_settings.governance_key().value)
        self.assertIn("could not read the Mezmo key", self.stderr.getvalue())
        mezmo_settings._read_store.cache_clear()
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("security")):
            self.assertIsNone(mezmo_settings.governance_key().value)
            # The ledger hook stays off rather than crash.
            self.assertIsNone(ledger.load_ledger_config())

    def test_a_key_file_others_can_read_or_a_symlink_is_refused(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes")
        self.save(key_store="file")
        path = self.save_key_file()
        self.assertEqual(mezmo_settings.governance_key().value, KEY)
        for mode in (0o640, 0o604, 0o620):
            with self.subTest(mode=oct(mode)):
                mezmo_settings._read_store.cache_clear()
                path.chmod(mode)
                self.assertIsNone(mezmo_settings.governance_key().value)
                self.assertIn("chmod 600", self.stderr.getvalue())
        path.chmod(0o600)
        real = self.dir / "elsewhere"
        path.rename(real)
        path.symlink_to(real)
        mezmo_settings._read_store.cache_clear()
        self.assertIsNone(mezmo_settings.governance_key().value)
        self.assertIn("symlink", self.stderr.getvalue())
        self.assertNotIn(KEY, self.stderr.getvalue())


class Ledger(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append((self.path, self.headers.get("Authorization"), self.rfile.read(length)))  # type: ignore[attr-defined]
        status, body = self.server.answer  # type: ignore[attr-defined]
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


class LedgerCheckTests(unittest.TestCase):
    def serve(self, answer: tuple) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Ledger)
        server.requests, server.answer = [], answer  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_the_empty_batch_passes_only_on_202_accepted_0(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "*"}):
            mezmo_setup.check_ledger(self.serve((202, {"accepted": 0})) + "/v1/audit/events", KEY)
            self.assertEqual(self.server.requests, [("/v1/audit/events", f"Token {KEY}", b"[]")])
            for answer in ((200, {"accepted": 0}), (202, {"accepted": 1}), (404, {}), (401, {})):
                with self.subTest(answer=answer), self.assertRaises(mezmo_setup.SetupError) as raised:
                    mezmo_setup.check_ledger(self.serve(answer) + "/v1/audit/events", KEY)
                self.assertNotIn(KEY, str(raised.exception))


class SetupTests(SettingsCase):
    def answers(self, governance: int = 404, ledger: tuple = (202, {"accepted": 0})) -> Any:
        self.sent: list[tuple] = []

        def send(method: str, url: str, key: str, body: Any) -> tuple:
            self.sent.append((method, url, key, body))
            return (governance, None) if method == "GET" else ledger

        return send

    def run_setup(self, platform: str, which: Any, fake: FakeStore, **kwargs: Any) -> tuple[int, str]:
        out = StringIO()
        with (
            patch("builtins.input", return_value=API),
            patch.object(mezmo_setup.getpass, "getpass", return_value=KEY),
            patch.object(mezmo_setup.shutil, "which", which),
            patch.object(subprocess, "run", fake),
            redirect_stdout(out),
            redirect_stderr(out),
        ):
            code = mezmo_setup.setup(send=self.answers(**kwargs), platform=platform)
        text = out.getvalue() + self.stderr.getvalue()
        self.assertNotIn(KEY, text)
        fake.assert_key_never_in_argv(self)
        return code, text

    def config(self) -> dict:
        return json.loads((self.dir / "codex.json").read_text(encoding="utf-8"))

    def test_macos_saves_the_key_in_the_keychain_through_stdin(self) -> None:
        fake = FakeStore()
        code, text = self.run_setup("darwin", lambda name: f"/usr/bin/{name}", fake)

        self.assertEqual(code, 0, text)
        self.assertEqual(fake.key, KEY)
        self.assertEqual(fake.calls[0][0], ["security", "-i"])
        self.assertIn("add-generic-password -U -s mezmo-ai-governance -a codex", fake.calls[0][1])
        self.assertIn("macOS Keychain", text)
        self.assertEqual(self.config(), {"api_url": API, "ledger_enabled": True, "key_store": "keychain"})
        self.assertNotIn(KEY, (self.dir / "codex.json").read_text(encoding="utf-8"))
        self.assertFalse((self.dir / "codex-key").exists())
        # Both checks ran with the key in the header only.
        self.assertEqual([(method, url.split("?")[0]) for method, url, _, _ in self.sent],
                         [("GET", API + "/governance/workflow/authorize"), ("POST", API + "/v1/audit/events")])
        self.assertEqual(self.sent[1][3], b"[]")

    def test_linux_saves_the_key_with_secret_tool_through_stdin(self) -> None:
        fake = FakeStore()
        code, text = self.run_setup("linux", lambda name: f"/usr/bin/{name}" if name == "secret-tool" else None, fake)

        self.assertEqual(code, 0, text)
        args, stdin = fake.calls[0]
        self.assertEqual(args[:2], ["secret-tool", "store"])
        self.assertEqual(stdin, KEY)
        self.assertEqual(self.config()["key_store"], "secret-tool")

    def test_without_a_store_the_key_file_is_0600_in_a_0700_folder(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes")
        fake = FakeStore()
        code, text = self.run_setup("win32", lambda name: None, fake)

        self.assertEqual(code, 0, text)
        self.assertEqual(fake.calls, [])
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.dir / "codex-key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.dir / "codex.json").stat().st_mode), 0o600)
        self.assertEqual(self.config()["key_store"], "file")
        self.assertEqual(mezmo_settings.governance_key().value, KEY)

    def test_a_failed_check_saves_nothing(self) -> None:
        for kwargs in ({"governance": 401}, {"governance": 500}, {"ledger": (404, {})}):
            with self.subTest(kwargs=kwargs):
                fake = FakeStore()
                code, text = self.run_setup("darwin", lambda name: f"/usr/bin/{name}", fake, **kwargs)
                self.assertEqual(code, 1)
                self.assertIn("Nothing was saved", text)
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.dir.exists())

    def test_remove_deletes_the_key_and_the_settings(self) -> None:
        fake = FakeStore()
        self.run_setup("darwin", lambda name: f"/usr/bin/{name}", fake)
        out = StringIO()
        with patch.object(subprocess, "run", fake), patch.object(mezmo_setup.shutil, "which", lambda name: name), redirect_stdout(out):
            self.assertEqual(mezmo_setup.main(["--remove"]), 0)
        self.assertIsNone(fake.key)
        self.assertEqual(fake.calls[-1][0][:2], ["security", "delete-generic-password"])
        self.assertFalse((self.dir / "codex.json").exists())

        self.run_setup("win32", lambda name: None, FakeStore())
        with redirect_stdout(out):
            self.assertEqual(mezmo_setup.main(["--remove"]), 0)
        self.assertFalse((self.dir / "codex-key").exists())
        self.assertFalse((self.dir / "codex.json").exists())

    def test_a_failed_remove_keeps_the_settings_and_exits_1(self) -> None:
        fake = FakeStore()
        self.run_setup("darwin", lambda name: f"/usr/bin/{name}", fake)
        fake.fail_deletes = True
        err = StringIO()
        with patch.object(subprocess, "run", fake), redirect_stdout(StringIO()), redirect_stderr(err):
            self.assertEqual(mezmo_setup.main(["--remove"]), 1)
        self.assertIn("could not delete the key", err.getvalue())
        self.assertEqual(fake.key, KEY)
        # codex.json still names the store, so a second --remove can finish the job.
        self.assertEqual(self.config()["key_store"], "keychain")
        fake.fail_deletes = False
        out = StringIO()
        with patch.object(subprocess, "run", fake), redirect_stdout(out):
            self.assertEqual(mezmo_setup.main(["--remove"]), 0)
        self.assertIsNone(fake.key)
        self.assertFalse((self.dir / "codex.json").exists())
        # A key already gone is not a failure.
        self.save(key_store="keychain")
        out = StringIO()
        with patch.object(subprocess, "run", fake), redirect_stdout(out):
            self.assertEqual(mezmo_setup.main(["--remove"]), 0)
        self.assertIn("no key was in", out.getvalue())

    def test_switching_stores_keeps_the_old_setup_until_the_new_settings_are_saved(self) -> None:
        self.run_setup("win32", lambda name: None, FakeStore())
        old_key = self.dir / "codex-key"
        self.assertTrue(old_key.exists())
        real_write = mezmo_setup.write_private

        def disk_full(path: Path, text: str) -> None:
            if path.name == "codex.json":
                raise OSError(28, "No space left on device")
            real_write(path, text)

        with patch.object(mezmo_setup, "write_private", disk_full):
            code, text = self.run_setup("darwin", lambda name: f"/usr/bin/{name}", FakeStore())
        self.assertEqual(code, 1)
        self.assertIn("previous settings are unchanged", text)
        self.assertEqual(self.config()["key_store"], "file")
        self.assertTrue(old_key.exists())
        mezmo_settings._read_store.cache_clear()
        self.assertEqual(mezmo_settings.governance_key().value, KEY)

        # Saved: then the old store's key goes.
        code, text = self.run_setup("darwin", lambda name: f"/usr/bin/{name}", FakeStore())
        self.assertEqual(code, 0, text)
        self.assertEqual(self.config()["key_store"], "keychain")
        self.assertFalse(old_key.exists())

    def test_a_keyring_that_never_answers_falls_back_to_the_key_file(self) -> None:
        fake = FakeStore(hang_secret_tool=True)
        code, text = self.run_setup("linux", lambda name: f"/usr/bin/{name}" if name == "secret-tool" else None, fake)

        self.assertEqual(code, 0, text)
        self.assertIn("did not answer", text)
        self.assertEqual(self.config()["key_store"], "file")
        self.assertEqual((self.dir / "codex-key").read_text(encoding="utf-8").strip(), KEY)


if __name__ == "__main__":
    unittest.main()
