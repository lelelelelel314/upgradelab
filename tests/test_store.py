from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from upgradelab.errors import EffectConflict, EffectNeedsReconciliation, LeaseLost
from upgradelab.models import EffectStatus, RunStatus, TaskSpec
from upgradelab.store import SQLiteRunStore


class FakeClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def task() -> TaskSpec:
    return TaskSpec(
        repo_path="/workspace/repo",
        base_commit="abc123",
        target_dependency="pydantic",
        target_version="2.0.0",
        test_command=("python", "-m", "pytest"),
    )


class SQLiteRunStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = SQLiteRunStore(Path(self.temp.name) / "runs.db", clock=self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claim_transition_and_audit(self) -> None:
        run = self.store.create_run(task(), run_id="run-1")
        lease = self.store.claim_next("worker-a", lease_seconds=10)
        assert lease is not None
        self.store.advance_stage(lease, "reproduce")
        completed = self.store.transition(
            lease,
            RunStatus.SUCCEEDED,
            stage="complete",
            result={"patch": "artifact://patch.diff"},
        )

        self.assertEqual(run.status, RunStatus.PENDING)
        self.assertEqual(completed.status, RunStatus.SUCCEEDED)
        self.assertIsNone(completed.lease_owner)
        self.assertEqual(completed.result, {"patch": "artifact://patch.diff"})
        self.assertEqual(
            [event["type"] for event in self.store.events(run.id)],
            ["RUN_CREATED", "RUN_CLAIMED", "STAGE_STARTED", "RUN_TRANSITIONED"],
        )

    def test_expired_lease_takeover_fences_old_worker(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        old = self.store.claim_next("worker-a", lease_seconds=5)
        assert old is not None
        self.clock.advance(6)
        new = self.store.claim_next("worker-b", lease_seconds=5)
        assert new is not None

        self.assertGreater(new.fence_token, old.fence_token)
        with self.assertRaises(LeaseLost):
            self.store.advance_stage(old, "stale-write")
        self.store.advance_stage(new, "recovered")
        self.assertEqual(self.store.events("run-1")[-2]["type"], "LEASE_RECOVERED")

    def test_named_acquire_is_reentrant_for_same_orchestrator(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        first = self.store.acquire_run("run-1", "outer-run", lease_seconds=5)
        same = self.store.acquire_run("run-1", "outer-run", lease_seconds=5)
        self.assertEqual(first, same)

        with self.assertRaises(LeaseLost):
            self.store.acquire_run("run-1", "another-run", lease_seconds=5)

        self.clock.advance(6)
        recovered = self.store.acquire_run("run-1", "another-run", lease_seconds=5)
        self.assertGreater(recovered.fence_token, first.fence_token)

    def test_succeeded_effect_is_replayed_without_execution(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        lease = self.store.claim_next("worker-a")
        assert lease is not None
        first = self.store.begin_effect(
            lease,
            operation_key="apply-patch-1",
            name="workspace.apply_patch",
            request={"patch_sha": "deadbeef"},
        )
        self.assertTrue(first.execute)
        self.store.finish_effect(
            lease,
            operation_key="apply-patch-1",
            succeeded=True,
            result={"workspace_sha": "cafe"},
        )
        replay = self.store.begin_effect(
            lease,
            operation_key="apply-patch-1",
            name="workspace.apply_patch",
            request={"patch_sha": "deadbeef"},
        )

        self.assertFalse(replay.execute)
        self.assertEqual(replay.effect.result, {"workspace_sha": "cafe"})

    def test_effect_request_cannot_change_under_same_key(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        lease = self.store.claim_next("worker-a")
        assert lease is not None
        self.store.begin_effect(
            lease,
            operation_key="apply-patch-1",
            name="workspace.apply_patch",
            request={"patch_sha": "one"},
        )
        with self.assertRaises(EffectConflict):
            self.store.begin_effect(
                lease,
                operation_key="apply-patch-1",
                name="workspace.apply_patch",
                request={"patch_sha": "two"},
            )

    def test_interrupted_effect_requires_evidence_reconciliation(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        old = self.store.claim_next("worker-a", lease_seconds=2)
        assert old is not None
        self.store.begin_effect(
            old,
            operation_key="apply-patch-1",
            name="workspace.apply_patch",
            request={"patch_sha": "deadbeef"},
        )
        self.clock.advance(3)
        new = self.store.claim_next("worker-b", lease_seconds=10)
        assert new is not None

        with self.assertRaises(EffectNeedsReconciliation):
            self.store.begin_effect(
                new,
                operation_key="apply-patch-1",
                name="workspace.apply_patch",
                request={"patch_sha": "deadbeef"},
            )
        reconciled = self.store.reconcile_effect(
            new,
            operation_key="apply-patch-1",
            succeeded=True,
            evidence={"workspace_sha": "cafe", "checked": True},
        )
        self.assertEqual(reconciled.status, EffectStatus.SUCCEEDED)
        self.assertFalse(
            self.store.begin_effect(
                new,
                operation_key="apply-patch-1",
                name="workspace.apply_patch",
                request={"patch_sha": "deadbeef"},
            ).execute
        )

    def test_checkpoint_binds_source_and_environment(self) -> None:
        self.store.create_run(task(), run_id="run-1")
        lease = self.store.claim_next("worker-a")
        assert lease is not None
        saved = self.store.record_checkpoint(
            lease,
            stage="verify",
            source_fingerprint="source-a",
            environment_fingerprint="env-a",
            payload={"tests": 12},
        )
        self.assertEqual(self.store.latest_checkpoint("run-1"), saved)


if __name__ == "__main__":
    unittest.main()
