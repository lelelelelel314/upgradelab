from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from upgradelab.context import ContextManifest
from upgradelab.models import RunStatus, TaskSpec
from upgradelab.pipeline import RepairPipeline
from upgradelab.report import write_html_report
from upgradelab.store import SQLiteRunStore
from upgradelab.workspace import LocalGitWorkspace, PatchCandidate


PATCH = textwrap.dedent(
    """\
    diff --git a/calc.py b/calc.py
    --- a/calc.py
    +++ b/calc.py
    @@ -1,2 +1,2 @@
     def add(left, right):
    -    return left - right
    +    return left + right
    """
)


class FixedRepairer:
    def __init__(self) -> None:
        self.context_paths: tuple[str, ...] = ()

    def propose(
        self,
        task: TaskSpec,
        failure: object,
        context: ContextManifest,
    ) -> PatchCandidate:
        self.context_paths = context.paths()
        return PatchCandidate(
            patch=PATCH,
            touched_files=("calc.py",),
            rationale="Correct the behavior required by the failing contract test.",
        )


class RepairPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "fixture"
        self.root.mkdir()
        (self.root / "calc.py").write_text(
            "def add(left, right):\n    return left - right\n", encoding="utf-8"
        )
        (self.root / "test_calc.py").write_text(
            "import unittest\n"
            "from calc import add\n\n"
            "class ContractTest(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )
        self._git("init")
        self._git("config", "user.email", "fixture@example.invalid")
        self._git("config", "user.name", "UpgradeLab Fixture")
        self._git("add", "calc.py", "test_calc.py")
        self._git("commit", "-m", "broken upgrade fixture")
        self.base_commit = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *args),
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )

    def test_real_workspace_patch_and_verification(self) -> None:
        store = SQLiteRunStore(Path(self.temp.name) / "runs.db")
        task = TaskSpec(
            repo_path=str(self.root),
            base_commit=self.base_commit,
            target_dependency="fixture-lib",
            target_version="2.0.0",
            test_command=(sys.executable, "-m", "unittest", "test_calc.py"),
        )
        store.create_run(task, run_id="fixture-run")
        lease = store.claim_next("worker-1", lease_seconds=30)
        assert lease is not None

        repairer = FixedRepairer()
        result = RepairPipeline(
            store,
            LocalGitWorkspace(self.root),
            repairer,
        ).execute(lease)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn("return left + right", (self.root / "calc.py").read_text(encoding="utf-8"))
        self.assertEqual(result.verification.exit_code, 0)
        checkpoint = store.latest_checkpoint("fixture-run")
        self.assertEqual(checkpoint.stage, "verify")
        self.assertTrue(checkpoint.payload["patch_evidence"]["bytecode_invalidated"])
        self.assertEqual(store.get_run("fixture-run").result["test_exit_code"], 0)
        self.assertIn("test_calc.py", repairer.context_paths)
        self.assertIn("calc.py", repairer.context_paths)
        event_types = [event["type"] for event in store.events("fixture-run")]
        self.assertIn("EFFECT_STARTED", event_types)
        self.assertIn("EFFECT_FINISHED", event_types)
        self.assertEqual(event_types[-1], "RUN_TRANSITIONED")
        report = write_html_report(store, "fixture-run", Path(self.temp.name) / "report.html")
        report_html = report.read_text(encoding="utf-8")
        self.assertIn("UpgradeLab run", report_html)
        self.assertIn("SUCCEEDED", report_html)
        self.assertIn("EFFECT_FINISHED", report_html)


if __name__ == "__main__":
    unittest.main()
