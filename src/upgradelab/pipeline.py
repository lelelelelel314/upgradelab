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
from .policy import PatchPolicy
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
    acceptance: CommandResult | None
    evidence: Mapping[str, object]


class RepairPipeline:
    STAGES = ("reproduce", "repair", "apply", "verify", "complete")

    def __init__(
        self,
        store: SQLiteRunStore,
        workspace: LocalGitWorkspace,
        repairer: Repairer,
        context_selector: FailureContextSelector | None = None,
        patch_policy: PatchPolicy | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.repairer = repairer
        self.patch_policy = patch_policy or PatchPolicy()
        self.context_selector = context_selector or FailureContextSelector(
            workspace.root,
            exclude=self.patch_policy.is_protected,
        )

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
        repair_task = TaskSpec.from_dict(task.public_dict())
        candidate = self.repairer.propose(repair_task, failure, selected_context)
        inspection = self.patch_policy.inspect(candidate)

        self.store.advance_stage(
            lease,
            "apply",
            evidence={
                "touched_files": list(inspection.paths),
                "patch_additions": inspection.additions,
                "patch_deletions": inspection.deletions,
                "policy_version": self.patch_policy.version,
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
            policy=self.patch_policy,
        )

        self.store.advance_stage(lease, "verify")
        verification = self.workspace.run(task.test_command)
        acceptance = None
        if verification.succeeded and task.acceptance_command is not None:
            acceptance = self.workspace.run(task.acceptance_command)
        environment = canonical_json_hash(
            {
                "python": platform.python_version(),
                "dependency": task.target_dependency,
                "version": task.target_version,
                "command": list(task.test_command),
                "acceptance_command": (
                    list(task.acceptance_command) if task.acceptance_command else None
                ),
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
                "acceptance": (
                    {
                        "exit_code": acceptance.exit_code,
                        "stdout": acceptance.stdout[-20_000:],
                        "stderr": acceptance.stderr[-20_000:],
                    }
                    if acceptance is not None
                    else None
                ),
                "patch_sha256": candidate.sha256,
                "patch_evidence": dict(patch_evidence),
            },
        )
        if not verification.succeeded:
            return self._fail(
                lease,
                "candidate failed verification",
                verification=verification,
                acceptance=acceptance,
                patch=candidate.patch,
            )
        if acceptance is not None and not acceptance.succeeded:
            return self._fail(
                lease,
                "candidate failed independent acceptance",
                verification=verification,
                acceptance=acceptance,
                patch=candidate.patch,
            )

        evidence = {
            "source_fingerprint": source,
            "environment_fingerprint": environment,
            "patch_sha256": candidate.sha256,
            "test_exit_code": verification.exit_code,
            "acceptance_exit_code": acceptance.exit_code if acceptance is not None else None,
            "touched_files": list(candidate.touched_files),
        }
        completed = self.store.transition(
            lease,
            RunStatus.SUCCEEDED,
            stage="complete",
            result=evidence,
        )
        return PipelineResult(
            completed.id,
            completed.status,
            candidate.patch,
            verification,
            acceptance,
            evidence,
        )

    def _fail(
        self,
        lease: Lease,
        message: str,
        *,
        verification: CommandResult | None = None,
        acceptance: CommandResult | None = None,
        patch: str | None = None,
    ) -> PipelineResult:
        failed = self.store.transition(
            lease,
            RunStatus.FAILED,
            stage="failed",
            error=message,
        )
        return PipelineResult(
            failed.id,
            failed.status,
            patch,
            verification,
            acceptance,
            {"error": message},
        )
