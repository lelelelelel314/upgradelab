"""Dependency repair pipeline built over the durable backend."""

from __future__ import annotations

import platform
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Protocol

from .context import ContextManifest, FailureContextSelector
from .errors import EffectNeedsReconciliation, LeaseLost
from .fingerprints import canonical_json_hash
from .models import Lease, RunStatus, TaskSpec
from .store import SQLiteRunStore
from .workspace import CommandResult, LocalGitWorkspace, PatchCandidate


class Repairer(Protocol):
    def propose(
        self,
        task: TaskSpec,
        failure: CommandResult,
        context: ContextManifest,
    ) -> PatchCandidate: ...


@dataclass(frozen=True, slots=True)
class PipelineResult:
    run_id: str
    status: RunStatus
    patch: str | None
    verification: CommandResult | None
    evidence: Mapping[str, object]


class RepairPipeline:
    STAGES = ("reproduce", "repair", "apply", "verify", "complete")

    def __init__(
        self,
        store: SQLiteRunStore,
        workspace: LocalGitWorkspace,
        repairer: Repairer,
        context_selector: FailureContextSelector | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.repairer = repairer
        self.context_selector = context_selector or FailureContextSelector(workspace.root)

    def execute(self, lease: Lease) -> PipelineResult:
        try:
            return self._execute(lease)
        except (EffectNeedsReconciliation, LeaseLost):
            raise
        except Exception as error:
            return self._fail(lease, f"{type(error).__name__}: {error}")

    def _execute(self, lease: Lease) -> PipelineResult:
        run = self.store.get_run(lease.run_id)
        task = run.task
        if self.workspace.git_head() != task.base_commit:
            return self._fail(lease, "workspace HEAD differs from task base_commit")

        self.store.advance_stage(lease, "reproduce")
        failure = self.workspace.run(task.test_command)
        if failure.succeeded:
            return self._fail(lease, "upgrade task is not reproducibly failing")

        self.store.advance_stage(lease, "repair")
        selected_context = self.context_selector.select(failure.stdout + "\n" + failure.stderr)
        candidate = self.repairer.propose(task, failure, selected_context)

        self.store.advance_stage(
            lease,
            "apply",
            evidence={
                "touched_files": list(candidate.touched_files),
                "context_files": [asdict(item) for item in selected_context.files],
                "context_bytes": selected_context.total_bytes,
                "context_truncated": selected_context.truncated,
            },
        )
        patch_evidence = self.workspace.apply_patch(
            self.store,
            lease,
            candidate,
            operation_key="candidate-1/apply-patch",
        )

        self.store.advance_stage(lease, "verify")
        verification = self.workspace.run(task.test_command)
        environment = canonical_json_hash(
            {
                "python": platform.python_version(),
                "dependency": task.target_dependency,
                "version": task.target_version,
                "command": list(task.test_command),
            }
        )
        source = self.workspace.source_fingerprint(candidate.touched_files)
        self.store.record_checkpoint(
            lease,
            stage="verify",
            source_fingerprint=source,
            environment_fingerprint=environment,
            payload={
                "exit_code": verification.exit_code,
                "stdout": verification.stdout[-20_000:],
                "stderr": verification.stderr[-20_000:],
                "patch_sha256": candidate.sha256,
                "patch_evidence": dict(patch_evidence),
            },
        )
        if not verification.succeeded:
            return self._fail(
                lease,
                "candidate failed verification",
                verification=verification,
                patch=candidate.patch,
            )

        evidence = {
            "source_fingerprint": source,
            "environment_fingerprint": environment,
            "patch_sha256": candidate.sha256,
            "test_exit_code": verification.exit_code,
            "touched_files": list(candidate.touched_files),
        }
        completed = self.store.transition(
            lease,
            RunStatus.SUCCEEDED,
            stage="complete",
            result=evidence,
        )
        return PipelineResult(completed.id, completed.status, candidate.patch, verification, evidence)

    def _fail(
        self,
        lease: Lease,
        message: str,
        *,
        verification: CommandResult | None = None,
        patch: str | None = None,
    ) -> PipelineResult:
        failed = self.store.transition(
            lease,
            RunStatus.FAILED,
            stage="failed",
            error=message,
        )
        return PipelineResult(failed.id, failed.status, patch, verification, {"error": message})
