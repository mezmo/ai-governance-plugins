"""The adapter copies shared code so it installs on its own. Copies must not drift."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "mezmo_governance_claude_agent_sdk"
REPO = Path(__file__).resolve().parents[3]
LANGCHAIN_CLIENT = REPO / "langchain" / "mezmo_governance" / "client.py"
CODEX_HOOK = REPO / "codex" / "mezmo_governance_hook.py"
# The audit and deny helpers in hooks.py match the Codex hook's, whose deny
# output has the same hookSpecificOutput shape as the SDK's.
HOOK_DEFINITIONS = (
    "DEFAULT_AUDIT_LOG",
    "MAX_RESULT_PREVIEW_CHARACTERS",
    "audit_log_path",
    "result_preview",
    "result_has_known_error",
    "append_audit",
    "deny_output",
)


def normalized_client(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'\A""".*"""$', '"""<docstring>"""', text, count=1, flags=re.M)
    text = re.sub(r'^AGENT = ".*"$', 'AGENT = "<agent>"', text, count=1, flags=re.M)
    text = re.sub(r'^ {8}"kind": "[a-z_]+",$', '        "kind": "<origin>",', text, count=1, flags=re.M)
    return re.sub(r'^ {8}"reason": "\w+",$', '        "reason": "<event>",', text, count=1, flags=re.M)


def top_level_definitions(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = ast.dump(node.value)
    return found


class DriftTests(unittest.TestCase):
    def test_client_differs_from_the_langchain_copy_only_in_agent_and_origin(self) -> None:
        if not LANGCHAIN_CLIENT.is_file():
            self.skipTest("the LangChain adapter is not checked out next to this one")
        ours = normalized_client(PACKAGE / "client.py")
        for placeholder in ('"<docstring>"', '"<agent>"', '"<origin>"', '"<event>"'):
            self.assertIn(placeholder, ours)
        self.assertEqual(ours, normalized_client(LANGCHAIN_CLIENT))

    def test_audit_helpers_match_the_codex_hook(self) -> None:
        if not CODEX_HOOK.is_file():
            self.skipTest("the Codex adapter is not checked out next to this one")
        ours = top_level_definitions(PACKAGE / "hooks.py")
        theirs = top_level_definitions(CODEX_HOOK)
        for name in HOOK_DEFINITIONS:
            with self.subTest(definition=name):
                self.assertIn(name, ours)
                self.assertEqual(ours[name], theirs.get(name))


if __name__ == "__main__":
    unittest.main()
