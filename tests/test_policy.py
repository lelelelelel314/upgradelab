from __future__ import annotations

import unittest

from upgradelab.errors import PatchPolicyViolation
from upgradelab.policy import PatchPolicy
from upgradelab.workspace import PatchCandidate


VALID_PATCH = """diff --git a/pkg/model.py b/pkg/model.py
--- a/pkg/model.py
+++ b/pkg/model.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


class PatchPolicyTests(unittest.TestCase):
    def test_inspects_declared_source_patch(self) -> None:
        inspection = PatchPolicy().inspect(
            PatchCandidate(VALID_PATCH, ("pkg/model.py",), "update API")
        )

        self.assertEqual(inspection.paths, ("pkg/model.py",))
        self.assertEqual(inspection.additions, 1)
        self.assertEqual(inspection.deletions, 1)

    def test_rejects_false_touched_file_declaration(self) -> None:
        with self.assertRaisesRegex(PatchPolicyViolation, "do not match"):
            PatchPolicy().inspect(PatchCandidate(VALID_PATCH, ("safe.py",), "hide target"))

    def test_rejects_test_modification(self) -> None:
        patch = VALID_PATCH.replace("pkg/model.py", "tests/test_model.py")
        with self.assertRaisesRegex(PatchPolicyViolation, "protected paths"):
            PatchPolicy().inspect(
                PatchCandidate(patch, ("tests/test_model.py",), "weaken assertion")
            )

    def test_rejects_ci_and_benchmark_verifier_modifications(self) -> None:
        for protected_path in (
            ".gitlab-ci.yml",
            ".circleci/config.yml",
            "benchmarks/case_1/verifier.py",
            "pkg/conftest.py",
        ):
            with self.subTest(path=protected_path), self.assertRaisesRegex(
                PatchPolicyViolation, "protected paths"
            ):
                patch = VALID_PATCH.replace("pkg/model.py", protected_path)
                PatchPolicy().inspect(
                    PatchCandidate(patch, (protected_path,), "weaken the verification path")
                )

    def test_rejects_safe_diff_header_with_protected_target_header(self) -> None:
        patch = VALID_PATCH.replace("+++ b/pkg/model.py", "+++ b/tests/test_model.py")
        with self.assertRaises(PatchPolicyViolation):
            PatchPolicy().inspect(PatchCandidate(patch, ("pkg/model.py",), "misdirect patch"))

    def test_rejects_symlink_patch(self) -> None:
        patch = (
            "diff --git a/pkg/link.py b/pkg/link.py\n"
            "new file mode 120000\n--- /dev/null\n+++ b/pkg/link.py\n"
        )
        with self.assertRaisesRegex(PatchPolicyViolation, "symlink"):
            PatchPolicy().inspect(PatchCandidate(patch, ("pkg/link.py",), "link"))


if __name__ == "__main__":
    unittest.main()
