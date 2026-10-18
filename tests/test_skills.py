"""Contract tests over every shipped skill file (everything is a skill)."""
from __future__ import annotations

import unittest
from pathlib import Path

from core import tools as tools_mod


class ShippedSkillsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = {skill["name"]: skill for skill in tools_mod.discover_skills()}

    def test_expected_skills_present(self):
        for name in ("shell", "read_file", "write_file", "screenshot", "chrome_dom",
                     "mouse", "keyboard", "task_done", "task_blocked", "add_skill"):
            self.assertIn(name, self.skills)

    def test_every_skill_declares_the_contract(self):
        self.assertGreaterEqual(len(self.skills), 10)
        for name, skill in self.skills.items():
            with self.subTest(skill=name):
                module = skill["module"]
                self.assertEqual(name, Path(skill["path"]).stem)
                self.assertTrue(str(getattr(module, "DESCRIPTION", "")).strip())
                parameters = getattr(module, "PARAMETERS", None)
                self.assertIsInstance(parameters, dict)
                self.assertEqual(parameters.get("type"), "object")
                self.assertTrue(callable(getattr(module, "HANDLER", None))
                                or callable(getattr(module, "run", None)))

    def test_marker_skills_return_markers(self):
        self.assertEqual(self.skills["task_done"]["module"].HANDLER(summary="x"), "TASK_DONE: x")
        self.assertEqual(self.skills["task_blocked"]["module"].HANDLER(reason="y"), "TASK_BLOCKED: y")


if __name__ == "__main__":
    unittest.main()