"""mezmo_setup.py and mezmo_settings.py: where the hooks find the URL and key, and how setup saves them.

No test touches the real Keychain, keyring or ~/.mezmo: the settings folder is a temp folder and every
security / secret-tool command is a stand-in that records its argv and stdin.
"""

from __future__ import annotations

import ast
import json
import os
import re
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
import mezmo_key_file
import mezmo_settings
import mezmo_setup

KEY = "canary-key-5d1c8e0a"
ADAPTER_DIR = Path(__file__).resolve().parent
REAL_RUN = subprocess.run
REPO = ADAPTER_DIR.parent
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
        # Never the real Codex config.
        self.codex = Path(scratch.name) / ".codex"
        os.environ["CODEX_HOME"] = str(self.codex)
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

    def test_a_codex_json_that_cannot_be_read_still_counts_as_configured(self) -> None:
        # Only a missing file means never set up; a broken one must keep governance failing closed.
        self.assertFalse(mezmo_settings.governance_configured())
        mezmo_settings.settings_dir().mkdir(parents=True, exist_ok=True)
        for broken in ("{not json", "[]", "{}", '{"key_store": "bogus"}', ""):
            with self.subTest(broken=broken):
                mezmo_settings.config_path().write_text(broken, encoding="utf-8")
                self.assertTrue(mezmo_settings.governance_configured())

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

    def run_setup(self, platform: str, which: Any, fake: FakeStore, mcp: str = "", **kwargs: Any) -> tuple[int, str]:
        out = StringIO()
        with (
            # A person at a terminal answers each prompt.
            patch.object(mezmo_setup, "interactive", lambda: True),
            patch("builtins.input", lambda prompt: API if "API URL" in prompt else mcp),
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


class NonInteractiveTests(SettingsCase):
    """The flags the mezmo-setup skill runs: no prompt, the key from a private file or the store."""

    def setUp(self) -> None:
        super().setUp()
        self.sent: list = []
        self.governance = 404
        self.which = lambda name: None

        def send(method: str, url: str, key: str, body: Any) -> tuple:
            self.sent.append((method, url, key, body))
            return (self.governance, None) if method == "GET" else (202, {"accepted": 0})

        self.fake = FakeStore()
        for patcher in (
            patch.object(mezmo_setup, "http", send),
            patch.object(subprocess, "run", self.fake),
            patch.object(mezmo_setup.shutil, "which", lambda name: self.which(name)),
            patch("builtins.input", side_effect=AssertionError("asked")),
            patch.object(mezmo_setup.getpass, "getpass", side_effect=AssertionError("asked")),
            # As when Codex runs it: no terminal, whatever runs the tests.
            patch.object(mezmo_setup, "interactive", lambda: False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def key_file(self, key: str = KEY, mode: int = 0o600) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="mezmo-key-", dir=self.dir.parent))
        folder.chmod(0o700)
        path = folder / "key"
        path.write_text(key + "\n", encoding="utf-8")
        path.chmod(mode)
        return path

    def main(self, *argv: str) -> tuple:
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = mezmo_setup.main(list(argv))
        text = out.getvalue() + self.stderr.getvalue()
        self.assertNotIn(KEY, text)
        self.fake.assert_key_never_in_argv(self)
        return code, text

    def config(self) -> dict:
        return json.loads((self.dir / "codex.json").read_text(encoding="utf-8"))

    def codex_config(self) -> str:
        return (self.codex / "config.toml").read_text(encoding="utf-8")

    def test_without_a_terminal_a_missing_key_is_named_not_asked_for(self) -> None:
        # With or without flags: input and getpass would fail this test if they were called.
        for argv in (("--api-url", API), ()):
            with self.subTest(argv=argv):
                code, text = self.main(*argv)
                self.assertEqual(code, 1, text)
                self.assertIn("no key to save: run this in a terminal to be asked for it, or give --key-file or --keep-key", text)
                self.assertFalse((self.dir / "codex.json").exists())

    def test_status_tells_a_missing_key_from_one_it_cannot_check(self) -> None:
        self.assertEqual(self.main("--api-url", API, "--key-file", str(self.key_file()), "--delete-key-file")[0], 0)
        self.assertEqual(self.config()["key_store"], "file")
        self.assertIn("key: saved in", self.main("--status")[1])
        # The key file is readable even inside Codex's sandbox, so a deleted one is missing.
        mezmo_settings._read_store.cache_clear()
        mezmo_settings.key_path().unlink()
        mezmo_settings._read_store.cache_clear()
        text = self.main("--status")[1]
        self.assertIn("key: none saved", text)
        self.assertNotIn("checked from here", text)
        # The Keychain answers "not found" inside the sandbox whether or not it holds the key.
        for store in ("keychain", "secret-tool"):
            with self.subTest(store=store):
                path = mezmo_settings.config_path()
                path.write_text(json.dumps({**self.config(), "key_store": store}), encoding="utf-8")
                mezmo_settings._read_store.cache_clear()
                text = self.main("--status")[1]
                self.assertIn("but it can't be checked from here, as inside Codex's sandbox", text)
                self.assertNotIn("key: saved in", text)

    def test_at_a_terminal_the_flags_leave_out_only_what_they_give(self) -> None:
        # The command the setup skill hands out: --api-url alone, for an API with no known MCP URL.
        asked: list[str] = []

        def answer(prompt: str) -> str:
            asked.append(prompt)
            return "https://mcp.example.test/mcp"

        with patch.object(mezmo_setup, "interactive", lambda: True), patch("builtins.input", answer), \
                patch.object(mezmo_setup.getpass, "getpass", return_value=KEY):
            code, text = self.main("--api-url", API)
        self.assertEqual(code, 0, text)
        self.assertEqual(asked, ["Mezmo MCP URL (blank to skip the MCP): "])
        self.assertEqual(self.config()["api_url"], API)
        self.assertEqual(mezmo_settings.governance_key().value, KEY)
        self.assertIn('url = "https://mcp.example.test/mcp"', self.codex_config())

    def test_a_key_file_saves_what_the_prompt_saves_and_is_deleted(self) -> None:
        path = self.key_file()
        code, text = self.main("--api-url", API, "--key-file", str(path), "--delete-key-file")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.config(), {"api_url": API, "ledger_enabled": True, "key_store": "file"})
        self.assertEqual(mezmo_settings.governance_key().value, KEY)
        self.assertEqual([key for _, _, key, _ in self.sent], [KEY, KEY])
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_the_key_file_is_deleted_when_setup_fails(self) -> None:
        self.governance = 401
        path = self.key_file()
        code, text = self.main("--api-url", API, "--key-file", str(path), "--delete-key-file")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was saved", text)
        self.assertFalse(path.exists())
        self.assertFalse(self.dir.exists())

    def test_a_loose_or_linked_key_file_is_refused(self) -> None:
        loose = self.key_file(mode=0o640)
        real = self.key_file()
        link = real.parent / "link"
        link.symlink_to(real)
        for path in (loose, link):
            with self.subTest(path=path.name):
                code, text = self.main("--api-url", API, "--key-file", str(path))
                self.assertEqual(code, 1)
                self.assertIn("key file", text.lower())
                self.assertEqual(self.sent, [])
                self.assertFalse(self.dir.exists())

    def test_keep_key_uses_the_saved_key_without_writing_it_again(self) -> None:
        self.which = lambda name: f"/usr/bin/{name}"
        self.fake.key = KEY
        with patch.object(sys, "platform", "darwin"):
            code, text = self.main("--api-url", API, "--keep-key")
        self.assertEqual(code, 0, text)
        self.assertIn("kept the key in the macOS Keychain", text)
        self.assertEqual(self.config()["key_store"], "keychain")
        self.assertNotIn(["security", "-i"], [args for args, _ in self.fake.calls])
        self.assertEqual([key for _, _, key, _ in self.sent], [KEY, KEY])

        mezmo_settings._read_store.cache_clear()
        self.fake.key = None
        code, text = self.main("--api-url", API, "--keep-key")
        self.assertEqual(code, 1)
        self.assertIn("no Mezmo key is saved", text)

    def test_status_never_shows_the_key(self) -> None:
        self.assertEqual(self.main("--status")[0], 1)
        self.main("--api-url", API, "--key-file", str(self.key_file()), "--delete-key-file")
        code, text = self.main("--status")
        self.assertEqual(code, 0)
        self.assertIn(f"API URL: {API} (saved)", text)
        self.assertIn("key: saved in", text)

    def test_production_writes_the_mezmo_mcp_server_and_keeps_the_rest_of_config_toml(self) -> None:
        self.codex.mkdir()
        before = 'model = "x"\n\n[mcp_servers.other]\nurl = "https://other.example.test/mcp"\n'
        (self.codex / "config.toml").write_text(before, encoding="utf-8")
        (self.codex / "config.toml").chmod(0o600)

        code, text = self.main("--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        config = self.codex_config()
        self.assertTrue(config.startswith(before))
        self.assertNotIn(KEY, config)
        self.assertEqual((self.codex / "config.toml.mezmo-backup").read_text(encoding="utf-8"), before)
        self.assertEqual(stat.S_IMODE((self.codex / "config.toml").stat().st_mode), 0o600)
        self.assertIn(
            '\n\n# Written by Mezmo setup: run it again to change this table, or with --remove to delete it.\n'
            '[mcp_servers.mezmo]\nurl = "https://mcp.mezmo.com/mcp"\n'
            f'http_headers_helper = "python3 {self.dir}/codex-mcp/mezmo_mcp_headers.py"\n'
            'enabled_tools = ["get_ledger_investigation", "list_ledger_entries"]\ndefault_tools_approval_mode = "auto"\n',
            config,
        )
        parsed = mezmo_setup.tomllib.loads(config) if mezmo_setup.tomllib else None
        if parsed is not None:
            server = parsed["mcp_servers"]["mezmo"]
            self.assertEqual(server["url"], "https://mcp.mezmo.com/mcp")
            self.assertEqual(server["enabled_tools"], ["get_ledger_investigation", "list_ledger_entries"])
            self.assertEqual(server["default_tools_approval_mode"], "auto")
            self.assertEqual(server["http_headers_helper"], mezmo_setup.helper_command())
            self.assertEqual(set(server), {"url", "enabled_tools", "default_tools_approval_mode", "http_headers_helper"})
            self.assertEqual(parsed["mcp_servers"]["other"], {"url": "https://other.example.test/mcp"})

        # Again, with another URL: still one table, in the same place.
        code, text = self.main("--keep-key", "--mcp-url", "https://mcp.example.test/mcp")
        self.assertEqual(code, 0, text)
        config = self.codex_config()
        self.assertEqual(config.count("[mcp_servers.mezmo]"), 1)
        self.assertIn('url = "https://mcp.example.test/mcp"', config)
        self.assertTrue(config.startswith(before))

        # --remove takes out only setup's table.
        code, text = self.main("--remove")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.codex_config(), before)
        self.assertFalse(mezmo_setup.helper_dir().exists())

    def test_another_api_without_an_mcp_url_skips_the_mcp(self) -> None:
        code, text = self.main("--api-url", API, "--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        self.assertIn("skip  the Mezmo MCP", text)
        self.assertFalse((self.codex / "config.toml").exists())

    def test_setup_ends_with_the_restart_line_only_when_the_mcp_changed(self) -> None:
        restart = "Start a new Codex session to use the Mezmo MCP"
        code, text = self.main("--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        self.assertEqual(text.rstrip("\n").splitlines()[-1], restart)
        # The same table again: nothing for Codex to reload.
        code, text = self.main("--keep-key")
        self.assertEqual(code, 0, text)
        self.assertNotIn(restart, text)
        code, text = self.main("--keep-key", "--mcp-url", "https://mcp.example.test/mcp")
        self.assertEqual(code, 0, text)
        self.assertEqual(text.rstrip("\n").splitlines()[-1], restart)

    def test_setup_installs_a_launcher_that_runs_the_newest_installed_plugin(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX launcher")
        code, text = self.main("--api-url", API, "--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        launcher = self.dir / "bin" / "mezmo-setup"
        self.assertEqual(launcher, mezmo_settings.launcher_path())
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(launcher.parent.stat().st_mode), 0o700)

        # Stand-ins, so the real setup it runs never reaches the real Keychain or keyring.
        stubs = self.dir.parent / "stubs"
        stubs.mkdir()
        for name, code in (("security", 44), ("secret-tool", 1)):
            (stubs / name).write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
            (stubs / name).chmod(0o700)

        def run(*args: str) -> str:
            path = f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            env = {"PATH": path, "CODEX_HOME": str(self.codex), "HOME": str(self.dir.parent)}
            result = REAL_RUN([str(launcher), *args], env=env, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        # No plugin in Codex's cache: the plugin that installed it.
        self.assertIn("API URL:", run("--status"))
        # Versions compare as numbers, so 0.10.0 is newer than 0.9.1; arguments pass through.
        cache = self.codex / "plugins" / "cache" / "mezmo" / "mezmo-governance"
        for version in ("0.9.1", "0.10.0", "0.7.0"):
            (cache / version).mkdir(parents=True)
            (cache / version / "mezmo_setup.py").write_text(
                f"import sys\nprint({version!r}, sys.argv[1:])\n", encoding="utf-8"
            )
        self.assertEqual(run("--status"), "0.10.0 ['--status']\n")

    def test_moving_to_an_api_without_an_mcp_url_removes_setups_old_mcp(self) -> None:
        code, text = self.main("--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        self.assertIn("[mcp_servers.mezmo]", self.codex_config())

        # The saved key now belongs to another API; the old table would send it to the old MCP.
        code, text = self.main("--keep-key", "--api-url", API)
        self.assertEqual(code, 0, text)
        self.assertIn("removed the Mezmo MCP setup wrote for an earlier API URL", text)
        self.assertNotIn("[mcp_servers.mezmo]", self.codex_config())

    def test_a_hand_written_mezmo_server_is_kept_unless_replace_mcp(self) -> None:
        self.codex.mkdir()
        mine = '[mcp_servers.mezmo]\nurl = "https://mcp.mezmo.com/mcp"\nbearer_token_env_var = "MEZMO_MCP_KEY"\n\n[mcp_servers.mezmo.tools.x]\napproval_mode = "approve"\n\n[other]\na = 1\n'
        (self.codex / "config.toml").write_text(mine, encoding="utf-8")
        path = self.key_file()
        code, text = self.main("--key-file", str(path), "--delete-key-file")
        self.assertEqual(code, 4)
        self.assertIn("--replace-mcp", text)
        self.assertFalse(self.dir.exists() and (self.dir / "codex.json").exists())
        self.assertFalse(path.exists())
        self.assertEqual(self.codex_config(), mine)
        self.assertEqual(self.sent, [])

        code, text = self.main("--key-file", str(self.key_file()), "--delete-key-file", "--replace-mcp")
        self.assertEqual(code, 0, text)
        config = self.codex_config()
        self.assertNotIn("MEZMO_MCP_KEY", config)
        self.assertNotIn("tools.x", config)
        self.assertIn("[other]\na = 1", config)
        self.assertEqual((self.codex / "config.toml.mezmo-backup").read_text(encoding="utf-8"), mine)

    def test_the_helper_prints_the_header_from_the_saved_key_as_codex_runs_it(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX shell")
        code, text = self.main("--key-file", str(self.key_file()), "--delete-key-file")
        self.assertEqual(code, 0, text)
        command = mezmo_setup.helper_command()
        self.assertNotIn(str(ADAPTER_DIR), command)
        # Codex runs the helper with sh -c and reads a JSON object of header strings from stdout.
        env = {name: value for name, value in os.environ.items() if not name.startswith("MEZMO_")}
        env["HOME"] = str(self.dir.parent)
        result = REAL_RUN(["sh", "-c", command], env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"Authorization": f"Token {KEY}"})

        (self.dir / "codex-key").unlink()
        result = REAL_RUN(["sh", "-c", command], env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("set up Mezmo", result.stderr)


class KeyFileCopyTests(unittest.TestCase):
    def test_the_key_file_helpers_match_claude_codes_byte_for_byte(self) -> None:
        # The Codex plugin ships only codex/, so it carries a copy of these.
        def functions(path: Path) -> dict:
            source = path.read_text(encoding="utf-8")
            return {
                node.name: ast.get_source_segment(source, node)
                for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
            }

        copy = functions(ADAPTER_DIR / "mezmo_key_file.py")
        original = functions(REPO / "claude-code" / "scripts" / "mezmo_common.py")
        self.assertEqual(set(copy), {"_require_posix", "_private_key_fd", "read_key_file", "delete_key_file", "key_file_main"})
        for name, source in copy.items():
            with self.subTest(name=name):
                self.assertEqual(source, original[name])

    def test_create_key_file_makes_an_empty_private_file_and_prints_only_its_path(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX modes")
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(mezmo_key_file.key_file_main(["create-key-file"]), 0)
        path = Path(out.getvalue().strip())
        self.addCleanup(lambda: path.parent.rmdir() if path.parent.exists() else None)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertEqual(path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)


class SkillTests(unittest.TestCase):
    def skills(self) -> dict:
        manifest = json.loads((ADAPTER_DIR / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["skills"], "./skills/")
        found = {}
        for path in sorted((ADAPTER_DIR / "skills").glob("*/SKILL.md")):
            text = path.read_text(encoding="utf-8")
            match = re.match(r"---\n(.*?)\n---\n", text, re.S)
            self.assertIsNotNone(match, path)
            fields = dict(line.split(": ", 1) for line in match.group(1).splitlines())
            self.assertEqual(fields["name"], path.parent.name)
            self.assertTrue(fields["description"].strip())
            found[fields["name"]] = text
        return found

    def test_the_plugin_declares_the_setup_and_investigate_skills(self) -> None:
        skills = self.skills()
        self.assertEqual(set(skills), {"mezmo-setup", "mezmo-investigate"})
        for name, text in skills.items():
            with self.subTest(skill=name):
                # The skill finds the plugin from its own folder: no install path or version in it.
                self.assertNotIn("plugins/cache", text)
                self.assertIsNone(re.search(r"\b\d+\.\d+\.\d+\b", text))
                self.assertIn(f"<plugin root>/skills/{name}/SKILL.md", text)

    def test_setup_hands_the_user_one_terminal_command_that_asks_for_the_key(self) -> None:
        text = self.skills()["mezmo-setup"]
        # Codex can't save the key: its sandbox can't reach the Keychain, and a fresh Codex can't
        # approve running outside it. So the skill never runs setup and never takes the key.
        self.assertIn("Don't try to run setup\nyourself, inside or outside the sandbox.", text)
        for never in ("require_escalated", "with_additional_permissions", "--key-file", "create-key-file", "--key "):
            self.assertNotIn(never, text)
        # One stable command, never a versioned path, with only the choices the user made.
        self.assertIn("```bash\n~/.mezmo/bin/mezmo-setup\n```", text)
        self.assertEqual(mezmo_settings.launcher_path(), mezmo_settings.settings_dir() / "bin" / "mezmo-setup")
        self.assertNotIn("find ", text)
        self.assertNotIn("--disable hooks", text)
        handover = text[text.index("If it prints `ok`"):text.index("## 5. Check what it saved")]
        for flag in ("--no-ledger", "--keep-key", "--replace-mcp"):
            self.assertIn(flag, handover)
        self.assertNotIn("--api-url", handover)
        # It says what the command asks, and checks the result itself.
        self.assertIn("the MCP URL (only for\nanother API URL", text)
        self.assertIn("Run `--status` again (step 1)", text)
        # Both of setup's success lines, as mezmo_setup.py prints them.
        source = (ADAPTER_DIR / "mezmo_setup.py").read_text(encoding="utf-8")
        for line in ("saved the key in ", "kept the key in "):
            self.assertIn(f'"{line}...', text)
            self.assertIn(line, source)
        self.assertIn("/hooks", text)
        self.assertIn("Start a new Codex session to use the Mezmo MCP", text)
        self.assertIn("Start a new Codex session to use the Mezmo MCP", (ADAPTER_DIR / "mezmo_setup.py").read_text(encoding="utf-8"))

    def test_investigate_follows_the_readme_steps(self) -> None:
        text = self.skills()["mezmo-investigate"]
        readme = (ADAPTER_DIR / "README.md").read_text(encoding="utf-8")
        start = readme.index("## Incident investigations\n\nRecord")
        block = " ".join(readme[start:readme.index("```", start)].split()).lower()
        text = " ".join(text.split()).lower()
        for step in (
            'join --incident-key <key> --service <name>', "get_ledger_investigation", "close --incident-key <key>",
            "pagerduty:<incident id>", "one command per shell call", "stay on the incident's service and namespace",
        ):
            with self.subTest(step=step):
                self.assertIn(step, block)
                self.assertIn(step, text)



if __name__ == "__main__":
    unittest.main()
