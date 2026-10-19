"""Phase 8B: skill AST/schema/dry-run gates and three-strike quarantine."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from core import skill_sandbox
from tests.support import IsolatedCase

VALID_SKILL = '''\
DESCRIPTION = "Return a value."
PARAMETERS = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": [],
}


def run(args, runtime=None):
    return str((args or {}).get("value", "ok"))
'''


class SkillSandboxCase(IsolatedCase):
    def setUp(self) -> None:
        super().setUp()
        self.skills_dir = self.state / "skills"
        self.staging_dir = self.state / "staging"
        self.disabled_dir = self.state / "disabled"
        self.health_file = self.state / "skill_health.json"
        self.skills_dir.mkdir(parents=True)
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "SKILLS_DIR", self.skills_dir)
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "STAGING_DIR", self.staging_dir)
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "DISABLED_DIR", self.disabled_dir)
        )
        self._stack.enter_context(
            mock.patch.object(skill_sandbox, "HEALTH_FILE", self.health_file)
        )

    def health(self) -> dict:
        return json.loads(self.health_file.read_text(encoding="utf-8"))


class AstGateTests(SkillSandboxCase):
    def test_top_level_call_is_rejected(self):
        code = VALID_SKILL + '\nPath("side-effect.txt").write_text("x")\n'
        errors = skill_sandbox.check_skill_ast(code)
        self.assertTrue(any("forbidden top-level" in error for error in errors))

    def test_top_level_assignment_call_is_rejected(self):
        code = VALID_SKILL + '\nCACHE = dict()\n'
        errors = skill_sandbox.check_skill_ast(code)
        self.assertTrue(any("forbidden top-level Call" in error for error in errors))

    def test_forbidden_package_is_rejected(self):
        code = "import pywinauto\n" + VALID_SKILL
        errors = skill_sandbox.check_skill_ast(code)
        self.assertTrue(any("forbidden package 'pywinauto'" in error for error in errors))

    def test_dynamic_execution_is_rejected(self):
        code = VALID_SKILL.replace(
            'return str((args or {}).get("value", "ok"))',
            'return eval("1 + 1")',
        )
        errors = skill_sandbox.check_skill_ast(code)
        self.assertTrue(any("dynamic code execution" in error for error in errors))

    def test_missing_handler_contract_is_rejected(self):
        code = 'DESCRIPTION = "x"\nPARAMETERS = {"type": "object", "properties": {}}\n'
        errors = skill_sandbox.check_skill_ast(code)
        self.assertTrue(any("HANDLER" in error and "run" in error for error in errors))


class SchemaGateTests(unittest.TestCase):
    def test_valid_schema(self):
        self.assertEqual(
            skill_sandbox.validate_openai_parameters_schema(
                {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": [],
                }
            ),
            [],
        )

    def test_unknown_type_is_rejected(self):
        errors = skill_sandbox.validate_openai_parameters_schema(
            {"type": "object", "properties": {"bad": {"type": "strange"}}}
        )
        self.assertTrue(any("invalid" in error for error in errors))

    def test_required_must_reference_defined_property(self):
        errors = skill_sandbox.validate_openai_parameters_schema(
            {"type": "object", "properties": {}, "required": ["missing"]}
        )
        self.assertTrue(any("undefined properties" in error for error in errors))


class DeployGateTests(SkillSandboxCase):
    def test_valid_skill_is_deployed(self):
        result = skill_sandbox.verify_and_deploy_skill("demo_skill", VALID_SKILL)
        self.assertIn("created skill demo_skill.py", result)
        deployed = self.skills_dir / "demo_skill.py"
        self.assertTrue(deployed.is_file())
        self.assertIn("DESCRIPTION", deployed.read_text(encoding="utf-8"))

    def test_existing_skill_requires_modify_flag(self):
        (self.skills_dir / "demo_skill.py").write_text(VALID_SKILL, encoding="utf-8")
        result = skill_sandbox.verify_and_deploy_skill("demo_skill", VALID_SKILL)
        self.assertIn("already exists", result)

    def test_existing_skill_is_updated_with_modify_flag(self):
        deployed = self.skills_dir / "demo_skill.py"
        deployed.write_text("DESCRIPTION = 'old'\n", encoding="utf-8")

        result = skill_sandbox.verify_and_deploy_skill(
            "demo_skill", VALID_SKILL, overwrite=True
        )

        self.assertIn("updated skill demo_skill.py", result)
        self.assertIn("DESCRIPTION", deployed.read_text(encoding="utf-8"))
        self.assertNotIn("old", deployed.read_text(encoding="utf-8"))

    def test_frozen_core_skill_cannot_be_overwritten(self):
        result = skill_sandbox.verify_and_deploy_skill("shell", VALID_SKILL, overwrite=True)
        self.assertIn("protected core skill", result)

    def test_dry_run_import_failure_is_rejected(self):
        code = (
            "from core.definitely_missing_sandbox_module import nope\n" + VALID_SKILL
        )
        result = skill_sandbox.verify_and_deploy_skill("broken_skill", code)
        self.assertIn("SKILL_SANDBOX_DRYRUN_REJECTED", result)
        self.assertFalse((self.skills_dir / "broken_skill.py").exists())

    def test_syntax_error_is_rejected_before_staging(self):
        result = skill_sandbox.verify_and_deploy_skill("bad_syntax", "def nope(:\n")
        self.assertIn("SKILL_SANDBOX_AST_REJECTED", result)


class QuarantineTests(SkillSandboxCase):
    def test_three_consecutive_failures_quarantine_dynamic_skill(self):
        skill_path = self.skills_dir / "dynamic_tool.py"
        skill_path.write_text(VALID_SKILL, encoding="utf-8")

        self.assertIsNone(skill_sandbox.record_skill_outcome("dynamic_tool", False, "one"))
        self.assertIsNone(skill_sandbox.record_skill_outcome("dynamic_tool", False, "two"))
        note = skill_sandbox.record_skill_outcome("dynamic_tool", False, "three")

        self.assertIn("[SKILL_QUARANTINED]", note or "")
        self.assertFalse(skill_path.exists())
        self.assertTrue((self.disabled_dir / "dynamic_tool.py").is_file())
        entry = self.health()["dynamic_tool"]
        self.assertEqual(entry["status"], "quarantined")
        self.assertEqual(entry["consecutive_failures"], 3)

    def test_success_resets_failure_counter(self):
        skill_sandbox.record_skill_outcome("dynamic_tool", False, "one")
        skill_sandbox.record_skill_outcome("dynamic_tool", False, "two")
        skill_sandbox.record_skill_outcome("dynamic_tool", True)
        entry = self.health()["dynamic_tool"]
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["status"], "healthy")

    def test_success_is_persisted_for_dynamic_skill(self):
        skill_sandbox.record_skill_outcome("dynamic_tool", True)
        self.assertEqual(self.health()["dynamic_tool"]["status"], "healthy")

    def test_frozen_core_skill_is_never_tracked_or_moved(self):
        core_file = self.skills_dir / "mouse.py"
        core_file.write_text(VALID_SKILL, encoding="utf-8")
        for _ in range(3):
            self.assertIsNone(skill_sandbox.record_skill_outcome("mouse", False, "boom"))
        self.assertTrue(core_file.is_file())
        self.assertFalse(self.health_file.exists())


if __name__ == "__main__":
    unittest.main()
