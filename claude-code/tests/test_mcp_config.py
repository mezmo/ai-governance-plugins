import contextlib
import io
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import mcp_config as mcp  # noqa: E402

KEY = "sts_0123456789abcdef0123456789abcdef"
URL = "https://mcp.mezmo.com/mcp"
INITIALIZED = 'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"rmcp"}}}'


class FakeHttp:
    def __init__(self, status=200, body=INITIALIZED, error=None):
        self.calls = []
        self.status, self.body, self.error = status, body, error

    def __call__(self, method, url, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.error:
            raise self.error
        return self.status, self.body


def get_output(scope="User config (available in all your projects)", status="✔ Connected", url=URL, key=KEY):
    # What `claude mcp get mezmo` prints. It shows the header in full.
    return (
        f"mezmo:\n  Scope: {scope}\n  Status: {status}\n  Type: http\n  URL: {url}\n"
        f"  Headers:\n    Authorization: Bearer {key}\n\nTo remove this server, run: claude mcp remove mezmo -s user\n"
    )


class FakeClaude:
    """Stands in for `claude mcp add|get|remove`."""

    def __init__(self, existing=None, status="✔ Connected"):
        self.calls = []
        self.server = existing  # (scope text, url) or None
        self.status = status

    def __call__(self, args, stdin=None):
        self.calls.append(list(args))
        verb = args[1]
        if verb == "get":
            if self.server is None:
                return 1, 'No MCP server named "mezmo". Configured servers: '
            scope, url = self.server
            return 0, get_output(scope=scope, status=self.status, url=url)
        if verb == "add":
            if self.server is not None:
                return 1, "MCP server mezmo already exists in user config"
            self.server = ("User config (available in all your projects)", args[args.index("mezmo") + 1])
            return 0, 'Added HTTP MCP server mezmo with URL: https://mcp.mezmo.com/mcp to user config\nHeaders: {\n  "Authorization": "[REDACTED]"\n}'
        if verb == "remove":
            self.server = None
            return 0, 'Removed MCP server "mezmo" from user config'
        return 1, "unknown"


def run(*args, environ=None, http=None, claude=None):
    out, err = io.StringIO(), io.StringIO()
    claude = claude if claude is not None else FakeClaude()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mcp.main(list(args), environ=environ or {}, send=http or FakeHttp(), claude=claude)
    return code, out.getvalue(), err.getvalue(), claude


class CheckTest(unittest.TestCase):
    def test_initialize_with_the_key(self):
        http = FakeHttp()
        result = mcp.check_key(KEY, send=http)
        self.assertTrue(result.ok)
        call = http.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", URL))
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertIn("text/event-stream", call["headers"]["Accept"])
        self.assertEqual(json.loads(call["body"])["method"], "initialize")

    def test_rejected_key(self):
        result = mcp.check_key(KEY, send=FakeHttp(401, '{"message":"Unauthorized","code":"EAUTH"}'))
        self.assertFalse(result.ok)
        self.assertIn("rejected", result.detail)

    def test_unreachable(self):
        result = mcp.check_key(KEY, send=FakeHttp(error=OSError("timed out")))
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.detail)

    def test_a_pasted_bearer_prefix_is_dropped(self):
        self.assertEqual(mcp.clean_key(f"  Bearer {KEY} "), KEY)
        with self.assertRaises(ValueError):
            mcp.clean_key("   ")


class StatusParseTest(unittest.TestCase):
    def test_reads_scope_status_and_url_but_never_the_header(self):
        info = mcp.parse_get(get_output())
        self.assertEqual(info, {"scope": "user", "status": "✔ Connected", "connected": True, "url": URL})
        self.assertNotIn(KEY, json.dumps(info))

    def test_project_scope_and_failure(self):
        info = mcp.parse_get(get_output(scope="Project config (shared via .mcp.json)", status="✘ Failed to connect"))
        self.assertEqual((info["scope"], info["connected"]), ("project", False))


class CliTest(unittest.TestCase):
    def test_add_checks_the_key_then_adds_a_user_server(self):
        code, out, err, claude = run("add", "--key", KEY)
        self.assertEqual(code, 0, err)
        add = next(c for c in claude.calls if c[1] == "add")
        self.assertEqual(add, ["mcp", "add", "--scope", "user", "--transport", "http", "mezmo", URL,
                               "--header", f"Authorization: Bearer {KEY}"])
        self.assertIn("Connected", out)
        self.assertNotIn(KEY, out + err)
        self.assertIn("sts_…cdef", out)

    def test_add_reads_the_key_from_the_environment(self):
        code, _, err, claude = run("add", environ={"MEZMO_SERVICE_KEY": KEY})
        self.assertEqual(code, 0, err)
        self.assertTrue(any(c[1] == "add" for c in claude.calls))

    def test_add_refuses_a_rejected_key(self):
        code, _, err, claude = run("add", "--key", KEY, http=FakeHttp(401, "{}"))
        self.assertEqual(code, 3)
        self.assertIn("rejected", err)
        self.assertFalse(any(c[1] == "add" for c in claude.calls))

    def test_add_without_a_key(self):
        code, _, err, _ = run("add")
        self.assertEqual(code, 2)
        self.assertIn("MEZMO_SERVICE_KEY", err)

    def test_add_stops_when_a_server_is_already_there(self):
        claude = FakeClaude(existing=("User config (available in all your projects)", URL))
        code, _, err, claude = run("add", "--key", KEY, claude=claude)
        self.assertEqual(code, 4)
        self.assertIn("--force", err)
        self.assertFalse(any(c[1] in ("add", "remove") for c in claude.calls))

    def test_force_replaces_a_user_server(self):
        claude = FakeClaude(existing=("User config (available in all your projects)", "https://old.example.com/mcp"))
        code, _, err, claude = run("add", "--key", KEY, "--force", claude=claude)
        self.assertEqual(code, 0, err)
        verbs = [c[1] for c in claude.calls if c[1] in ("add", "remove")]
        self.assertEqual(verbs, ["remove", "add"])
        self.assertEqual(claude.server[1], URL)

    def test_force_leaves_a_project_server_alone(self):
        claude = FakeClaude(existing=("Project config (shared via .mcp.json)", URL))
        code, _, err, claude = run("add", "--key", KEY, "--force", claude=claude)
        self.assertEqual(code, 4)
        self.assertIn(".mcp.json", err)
        self.assertFalse(any(c[1] in ("add", "remove") for c in claude.calls))

    def test_remove(self):
        claude = FakeClaude(existing=("User config (available in all your projects)", URL))
        code, out, _, claude = run("remove", claude=claude)
        self.assertEqual(code, 0)
        self.assertIn(["mcp", "remove", "mezmo", "-s", "user"], claude.calls)
        self.assertIsNone(claude.server)
        self.assertIn("Removed", out)

    def test_remove_when_there_is_nothing(self):
        code, out, _, claude = run("remove")
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)
        self.assertFalse(any(c[1] == "remove" for c in claude.calls))

    def test_status_never_prints_the_key(self):
        claude = FakeClaude(existing=("User config (available in all your projects)", URL))
        code, out, _, _ = run("status", claude=claude)
        self.assertEqual(code, 0)
        self.assertIn("Connected", out)
        self.assertNotIn(KEY, out)
        code, out, _, _ = run("status", "--json", claude=claude)
        report = json.loads(out)
        self.assertTrue(report["configured"])
        self.assertTrue(report["connected"])
        self.assertTrue(report["is_mezmo"])
        self.assertNotIn(KEY, out)

    def test_status_when_not_set_up(self):
        code, out, _, _ = run("status")
        self.assertEqual(code, 0)
        self.assertIn("isn't set up", out)


if __name__ == "__main__":
    unittest.main()
