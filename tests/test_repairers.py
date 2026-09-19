from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from upgradelab.context import ContextFile, ContextManifest
from upgradelab.models import TaskSpec
from upgradelab.repairers import RepairerProtocolError, SubprocessRepairer
from upgradelab.workspace import CommandResult


class SubprocessRepairerTests(unittest.TestCase):
    def test_json_protocol_carries_bounded_context_and_parses_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "calc.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
            agent = root / "agent.py"
            agent.write_text(
                "import json, sys\n"
                "request = json.load(sys.stdin)\n"
                "assert 'acceptance_command' not in request['task']\n"
                "assert request['context'][0]['content'].startswith('def add')\n"
                "json.dump({'patch': 'diff --git a/calc.py b/calc.py\\n', "
                "'touched_files': ['calc.py'], 'rationale': 'fix contract'}, sys.stdout)\n",
                encoding="utf-8",
            )
            repairer = SubprocessRepairer((sys.executable, str(agent)), root)
            task = TaskSpec(
                repo_path=str(root),
                base_commit="abc",
                target_dependency="demo",
                target_version="2",
                test_command=("python", "test.py"),
                acceptance_command=("python", "hidden.py"),
            )
            failure = CommandResult(("python", "test.py"), 1, "", "boom", 0.1)
            context = ContextManifest((ContextFile("calc.py", "traceback", 31),), 31, False)

            candidate = repairer.propose(task, failure, context)

            self.assertEqual(candidate.touched_files, ("calc.py",))
            self.assertEqual(candidate.rationale, "fix contract")

    def test_rejects_non_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent = root / "agent.py"
            agent.write_text("print('not-json')\n", encoding="utf-8")
            repairer = SubprocessRepairer((sys.executable, str(agent)), root)
            task = TaskSpec(str(root), "abc", "demo", "2", ("python", "test.py"))
            failure = CommandResult(("python",), 1, "", "boom", 0.1)

            with self.assertRaises(RepairerProtocolError):
                repairer.propose(task, failure, ContextManifest((), 0, False))


if __name__ == "__main__":
    unittest.main()
