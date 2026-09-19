"""Optional OpenRath v2 workflow and runtime adapter.

Imports are intentionally lazy so the durable core stays usable without the
``openrath`` extra.
"""

from __future__ import annotations

import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .context import ContextFile, ContextManifest, FailureContextSelector
from .fingerprints import canonical_json_hash
from .models import RunStatus, TaskSpec
from .pipeline import Repairer
from .store import SQLiteRunStore
from .workspace import CommandResult, LocalGitWorkspace, PatchCandidate


class OpenRathUnavailable(RuntimeError):
    pass


def _api() -> dict[str, Any]:
    try:
        from rath.context import RunContext
        from rath.definition import EffectClass, RetryPolicy, step
        from rath.flow import Workflow
        from rath.runtime import LocalRuntime, SQLiteRunStore as RathSQLiteRunStore
        from rath.session import Session
    except ModuleNotFoundError as error:
        raise OpenRathUnavailable(
            "OpenRath is optional; install UpgradeLab with the 'openrath' extra"
        ) from error
    return {
        "RunContext": RunContext,
        "EffectClass": EffectClass,
        "RetryPolicy": RetryPolicy,
        "step": step,
        "Workflow": Workflow,
        "LocalRuntime": LocalRuntime,
        "RathSQLiteRunStore": RathSQLiteRunStore,
        "Session": Session,
    }


def build_dependency_repair_workflow(
    *,
    domain_store: SQLiteRunStore,
    workspace: LocalGitWorkspace,
    repairer: Repairer,
    task: TaskSpec,
    domain_run_id: str,
):
    """Build the four-step OpenRath workflow around UpgradeLab domain guards."""
    api = _api()
    step = api["step"]
    effects = api["EffectClass"]
    retry_policy = api["RetryPolicy"]
    workflow_base = api["Workflow"]
    session_type = api["Session"]
    selector = FailureContextSelector(workspace.root)

    class DependencyRepairWorkflow(workflow_base):
        def __init__(self) -> None:
            super().__init__()

        @step(entry=True, successors=("repair",), effects=effects.READ_ONLY)
        def reproduce(self, state, context):
            if workspace.git_head() != task.base_commit:
                raise RuntimeError("workspace HEAD differs from task base_commit")
            failure = workspace.run(task.test_command)
            if failure.succeeded:
                raise RuntimeError("upgrade task is not reproducibly failing")
            manifest = selector.select(failure.stdout + "\n" + failure.stderr)
            return {
                **state,
                "failure": asdict(failure),
                "context": {
                    "files": [asdict(item) for item in manifest.files],
                    "total_bytes": manifest.total_bytes,
                    "truncated": manifest.truncated,
                },
            }

        @step(
            successors=("apply",),
            effects=effects.NON_IDEMPOTENT,
            timeout_seconds=180,
        )
        def repair(self, state, context):
            failure = CommandResult(**state["failure"])
            raw_context = state["context"]
            manifest = ContextManifest(
                tuple(ContextFile(**item) for item in raw_context["files"]),
                raw_context["total_bytes"],
                raw_context["truncated"],
            )
            candidate = repairer.propose(task, failure, manifest)
            return {
                **state,
                "candidate": {
                    "patch": candidate.patch,
                    "touched_files": list(candidate.touched_files),
                    "rationale": candidate.rationale,
                },
            }

        @step(
            successors=("verify",),
            effects=effects.IDEMPOTENT,
            idempotency_key="upgradelab-apply-v1",
            retry=retry_policy(max_attempts=3, base_seconds=0.25),
        )
        def apply(self, state, context):
            candidate = PatchCandidate(
                patch=state["candidate"]["patch"],
                touched_files=tuple(state["candidate"]["touched_files"]),
                rationale=state["candidate"]["rationale"],
            )
            owner = f"openrath:{context.run_id}"
            lease = domain_store.acquire_run(domain_run_id, owner, lease_seconds=300)
            domain_store.advance_stage(
                lease,
                "apply",
                evidence={"patch_sha256": candidate.sha256},
            )
            evidence = workspace.apply_patch(
                domain_store,
                lease,
                candidate,
                operation_key=f"candidate/{candidate.sha256}/apply",
            )
            return {**state, "patch_evidence": dict(evidence)}

        @step(
            effects=effects.IDEMPOTENT,
            idempotency_key="upgradelab-verify-v1",
            retry=retry_policy(max_attempts=2, base_seconds=0.25),
        )
        def verify(self, state, context):
            owner = f"openrath:{context.run_id}"
            lease = domain_store.acquire_run(domain_run_id, owner, lease_seconds=300)
            candidate = PatchCandidate(
                patch=state["candidate"]["patch"],
                touched_files=tuple(state["candidate"]["touched_files"]),
                rationale=state["candidate"]["rationale"],
            )
            domain_store.advance_stage(lease, "verify")
            verification = workspace.run(task.test_command)
            source = workspace.source_fingerprint(candidate.touched_files)
            environment = canonical_json_hash(
                {
                    "python": platform.python_version(),
                    "dependency": task.target_dependency,
                    "version": task.target_version,
                    "command": list(task.test_command),
                }
            )
            domain_store.record_checkpoint(
                lease,
                stage="verify",
                source_fingerprint=source,
                environment_fingerprint=environment,
                payload={"verification": asdict(verification), "patch_sha256": candidate.sha256},
            )
            if not verification.succeeded:
                domain_store.transition(
                    lease,
                    RunStatus.FAILED,
                    stage="failed",
                    error="candidate failed verification",
                )
                raise RuntimeError("candidate failed verification")
            result = {
                "source_fingerprint": source,
                "environment_fingerprint": environment,
                "patch_sha256": candidate.sha256,
                "test_exit_code": verification.exit_code,
                "touched_files": list(candidate.touched_files),
            }
            domain_store.transition(
                lease,
                RunStatus.SUCCEEDED,
                stage="complete",
                result=result,
            )
            return {**state, "result": result}

        def forward(self, session: session_type) -> session_type:
            return session

    return DependencyRepairWorkflow()


@dataclass(slots=True)
class OpenRathRuntime:
    runtime: Any
    store: Any
    workflow: Any
    revision_id: UUID

    @classmethod
    def open(cls, path: str | Path, workflow: Any, *, revision_id: UUID | None = None):
        api = _api()
        selected_revision = revision_id or uuid4()
        store = api["RathSQLiteRunStore"](path)
        runtime = api["LocalRuntime"](store)
        runtime.register(workflow, revision_id=selected_revision)
        return cls(runtime, store, workflow, selected_revision)

    def submit(self, state: dict[str, Any], *, idempotency_key: str):
        api = _api()
        return self.runtime.submit(
            self.workflow,
            session_id=uuid4(),
            context=api["RunContext"].local(revision_id=self.revision_id),
            state=state,
            idempotency_key=idempotency_key,
        )

    def work_once(self, worker_id: str, *, max_steps: int | None = None):
        kwargs = {"worker_id": worker_id}
        if max_steps is not None:
            kwargs["max_steps"] = max_steps
        return self.runtime.work_once(**kwargs)

    def close(self) -> None:
        self.store.close()
