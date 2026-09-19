from __future__ import annotations

import sys
import tempfile
import types
import unittest
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from upgradelab.context import ContextManifest
from upgradelab.models import RunStatus, TaskSpec
from upgradelab.openrath import OpenRathRuntime, build_dependency_repair_workflow
from upgradelab.store import SQLiteRunStore
from upgradelab.workspace import CommandResult, PatchCandidate


class FakeWorkspace:
    def __init__(self, root: Path, head: str) -> None:
        self.root = root
        self.head = head
        self.calls = 0

    def git_head(self) -> str:
        return self.head

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls += 1
        if self.calls == 1:
            return CommandResult(argv, 1, "", "assertion failed", 0.1)
        return CommandResult(argv, 0, "ok", "", 0.1)

    def apply_patch(self, store, lease, candidate, *, operation_key):
        decision = store.begin_effect(
            lease,
            operation_key=operation_key,
            name="workspace.apply_patch",
            request={"patch_sha": candidate.sha256},
        )
        if decision.execute:
            return store.finish_effect(
                lease,
                operation_key=operation_key,
                succeeded=True,
                result={"applied": True},
            ).result
        return decision.effect.result

    def source_fingerprint(self, paths: tuple[str, ...]) -> str:
        return "source-fingerprint"


class FixedRepairer:
    def propose(self, task, failure, context: ContextManifest) -> PatchCandidate:
        return PatchCandidate(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
            ("a.py",),
            "fixture",
        )


def fake_rath_modules() -> dict[str, types.ModuleType]:
    package = types.ModuleType("rath")
    package.__path__ = []
    context = types.ModuleType("rath.context")
    definition = types.ModuleType("rath.definition")
    flow = types.ModuleType("rath.flow")
    runtime = types.ModuleType("rath.runtime")
    session = types.ModuleType("rath.session")

    class EffectClass:
        READ_ONLY = "READ_ONLY"
        IDEMPOTENT = "IDEMPOTENT"
        NON_IDEMPOTENT = "NON_IDEMPOTENT"

    class RetryPolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def step(**metadata):
        def decorate(function):
            function.openrath_metadata = metadata
            return function

        return decorate

    class Workflow:
        pass

    class Session:
        pass

    context.RunContext = object
    definition.EffectClass = EffectClass
    definition.RetryPolicy = RetryPolicy
    definition.step = step
    flow.Workflow = Workflow
    runtime.LocalRuntime = object
    runtime.SQLiteRunStore = object
    session.Session = Session
    return {
        "rath": package,
        "rath.context": context,
        "rath.definition": definition,
        "rath.flow": flow,
        "rath.runtime": runtime,
        "rath.session": session,
    }


class OpenRathWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(find_spec("rath"), "OpenRath optional dependency is not installed")
    def test_actual_openrath_runtime_executes_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = SQLiteRunStore(root / "domain.db")
            task = TaskSpec(str(root), "base", "library", "2.0", ("python", "tests.py"))
            store.create_run(task, run_id="domain-run")
            workflow = build_dependency_repair_workflow(
                domain_store=store,
                workspace=FakeWorkspace(root, "base"),
                repairer=FixedRepairer(),
                task=task,
                domain_run_id="domain-run",
            )
            runtime = OpenRathRuntime.open(root / "openrath.db", workflow)
            try:
                submitted = runtime.submit({}, idempotency_key="integration-v1")
                completed = runtime.work_once("integration-worker")
            finally:
                runtime.close()

            self.assertEqual(submitted.status.value, "queued")
            self.assertEqual(completed.status.value, "succeeded")
            self.assertEqual(store.get_run("domain-run").status, RunStatus.SUCCEEDED)

    def test_four_step_workflow_uses_domain_effect_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            sys.modules, fake_rath_modules(), clear=False
        ):
            root = Path(temp_dir)
            store = SQLiteRunStore(root / "domain.db")
            task = TaskSpec(str(root), "base", "library", "2.0", ("python", "tests.py"))
            store.create_run(task, run_id="domain-run")
            workflow = build_dependency_repair_workflow(
                domain_store=store,
                workspace=FakeWorkspace(root, "base"),
                repairer=FixedRepairer(),
                task=task,
                domain_run_id="domain-run",
            )
            context = SimpleNamespace(run_id="outer-run")

            state = workflow.reproduce({}, context)
            state = workflow.repair(state, context)
            state = workflow.apply(state, context)
            state = workflow.verify(state, context)

            self.assertEqual(store.get_run("domain-run").status, RunStatus.SUCCEEDED)
            self.assertEqual(state["result"]["test_exit_code"], 0)
            event_types = [event["type"] for event in store.events("domain-run")]
            self.assertIn("EFFECT_FINISHED", event_types)
            self.assertEqual(workflow.reproduce.openrath_metadata["entry"], True)
            self.assertEqual(workflow.apply.openrath_metadata["effects"], "IDEMPOTENT")


if __name__ == "__main__":
    unittest.main()
