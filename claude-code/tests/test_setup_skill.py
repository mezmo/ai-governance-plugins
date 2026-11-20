import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "setup" / "SKILL.md"
INVESTIGATE = ROOT / "skills" / "investigate" / "SKILL.md"


class SetupSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = " ".join(SKILL.read_text(encoding="utf-8").split())

    def test_install_is_enough_and_setup_is_optional(self) -> None:
        self.assertIn("Installing the plugin with a token in its options dialog turns on governance, the ledger and "
                      "MCP in that session; this setup isn't needed for them.", self.text)
        self.assertIn("`mcp__plugin_mezmo_mezmo__<tool>`", self.text)
        self.assertNotIn("mcp__mezmo__", self.text)

    def test_keeping_governance_needs_nothing_more(self) -> None:
        kept = self.text[self.text.index("#### Already configured"):self.text.index("#### Reuse a saved key")]
        self.assertIn("governance, the ledger and Mezmo MCP already work with it", kept)
        self.assertIn("`mcp_url` option", kept)

    def test_mcp_without_governance_saves_the_key_for_the_plugins_server(self) -> None:
        section = self.text[self.text.index("## 5. Mezmo MCP"):self.text.index("## 6. ")]
        self.assertIn("Run this section only when the user chose MCP without governance.", section)
        self.assertIn("governance_config.py\" configure --mcp-only", section)
        self.assertNotIn("mcp_config.py\" add", section)
        self.assertIn("`--use-saved-key`", section)
        self.assertIn("Run /reload-plugins", section)


class TestSkillTests(unittest.TestCase):
    def test_it_finds_the_plugins_tools_and_says_to_reload_not_restart(self) -> None:
        text = " ".join((ROOT / "skills" / "test" / "SKILL.md").read_text(encoding="utf-8").split())
        step = text[text.index("3. If the server is set up"):text.index("## Verify arrival")]
        self.assertIn("`mcp__plugin_mezmo_mezmo__get_ledger_investigation`", step)
        self.assertIn("Run /reload-plugins", step)
        self.assertNotIn("Restart Claude Code", step)


class InvestigateSkillTests(unittest.TestCase):
    def test_the_plugins_ledger_tool_is_pre_approved_and_its_absence_explained(self) -> None:
        text = INVESTIGATE.read_text(encoding="utf-8")
        self.assertIn("  - mcp__plugin_mezmo_mezmo__get_ledger_investigation\n", text)
        self.assertIn("If no `get_ledger_investigation` tool is available", text)

    def test_both_investigate_skills_take_closing_times_from_the_record(self) -> None:
        # A closing once said its checks ran at 19:45 when the entries said 19:42.
        for path in (INVESTIGATE, ROOT.parent / "codex" / "skills" / "mezmo-investigate" / "SKILL.md"):
            with self.subTest(path=path.parent.name):
                text = " ".join(path.read_text(encoding="utf-8").split())
                self.assertIn("Take any time you write from the investigation's entries", text)
                self.assertIn("never from memory, or leave times out", text)


if __name__ == "__main__":
    unittest.main()
