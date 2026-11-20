import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "setup" / "SKILL.md"


class SetupSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = " ".join(SKILL.read_text(encoding="utf-8").split())

    def test_keeping_governance_still_connects_the_mcp(self) -> None:
        # The install dialog configures governance, and only `configure` connects the MCP, so keeping
        # what's configured must still lead to the MCP section.
        kept = self.text[self.text.index("#### Already configured"):self.text.index("#### Reuse a saved key")]
        self.assertIn("go on to [Mezmo MCP](#5-mezmo-mcp) even if the user didn't choose MCP", kept)

    def test_the_mcp_section_is_skipped_only_when_mezmo_is_there(self) -> None:
        section = self.text[self.text.index("## 5. Mezmo MCP"):]
        self.assertIn("Skip this section only when the user didn't choose MCP and governance's `configure` ran", section)
        self.assertIn("If it shows `mezmo` set up for that URL and connecting", section)
        self.assertNotIn("if governance for Just me already connected it", self.text)
        # The key from the install dialog can't be read, so it's asked for once, or the Codex key is offered.
        self.assertIn("`--use-saved-key`", section)
        self.assertIn("can't read the key given in the plugin's install dialog", section)


if __name__ == "__main__":
    unittest.main()
