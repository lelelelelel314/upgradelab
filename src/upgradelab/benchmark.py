from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from .models import TaskSpec
from .pipeline import RepairPipeline
from .report import write_html_report
from .store import SQLiteRunStore
from .workspace import LocalGitWorkspace, PatchCandidate


PYDANTIC_PATTERN_PATCH = textwrap.dedent(
    """\
    diff --git a/profile.py b/profile.py
    --- a/profile.py
    +++ b/profile.py
    @@ -5 +5 @@ class Registration(BaseModel):
    -    username: str = Field(min_length=3, regex=r"^[a-z][a-z0-9_]+$")
    +    username: str = Field(min_length=3, pattern=r"^[a-z][a-z0-9_]+$")
    """
)


class PydanticPatternRepairer:
    """Deterministic baseline used to validate the benchmark and evidence path."""

    def propose(self, task, failure, context) -> PatchCandidate:
        if "profile.py" not in context.paths():
            raise RuntimeError("benchmark context did not select profile.py")
        return PatchCandidate(
            PYDANTIC_PATTERN_PATCH,
            ("profile.py",),
            "Replace the removed Field regex argument with the Pydantic v2 pattern argument.",
        )


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    case: str
    run_id: str
    status: str
    dependency_version: str
    duration_seconds: float
    visible_test_exit_code: int | None
    acceptance_exit_code: int | None
    patch_sha256: str | None
    report_path: str
    result_path: str


def run_pydantic_pattern_benchmark(
    output_root: str | Path,
    *,
    python_executable: str = sys.executable,
) -> BenchmarkResult:
    repository_root = Path(__file__).resolve().parents[2]
    case_root = repository_root / "benchmarks" / "pydantic_v2_field_pattern"
    fixture = case_root / "fixture"
    verifier = case_root / "verifier.py"
    if not fixture.is_dir() or not verifier.is_file():
        raise RuntimeError("benchmark files are missing")

    run_dir = Path(output_root).resolve() / f"pydantic-{uuid4().hex[:8]}"
    workspace_root = run_dir / "workspace"
    shutil.copytree(fixture, workspace_root)
    _git(workspace_root, "init")
    _git(workspace_root, "config", "user.email", "benchmark@upgradelab.invalid")
    _git(workspace_root, "config", "user.name", "UpgradeLab Benchmark")
    _git(workspace_root, "add", ".")
    _git(workspace_root, "commit", "-m", "pydantic v2 broken fixture")

    workspace = LocalGitWorkspace(workspace_root)
    store = SQLiteRunStore(run_dir / "runs.db")
    task = TaskSpec(
        repo_path=str(workspace_root),
        base_commit=workspace.git_head(),
        target_dependency="pydantic",
        target_version=version("pydantic"),
        test_command=(python_executable, "-m", "unittest", "test_visible.py"),
        acceptance_command=(python_executable, str(verifier)),
    )
    run = store.create_run(task)
    lease = store.claim_next("benchmark-worker", lease_seconds=120)
    assert lease is not None
    started = time.monotonic()
    pipeline_result = RepairPipeline(
        store,
        workspace,
        PydanticPatternRepairer(),
    ).execute(lease)
    duration = time.monotonic() - started
    report = write_html_report(store, run.id, run_dir / "report.html")
    stored = store.get_run(run.id)
    result_payload = stored.result or {}
    result_path = run_dir / "result.json"
    result = BenchmarkResult(
        case="pydantic-v2-field-pattern",
        run_id=run.id,
        status=stored.status.value,
        dependency_version=version("pydantic"),
        duration_seconds=duration,
        visible_test_exit_code=result_payload.get("test_exit_code"),
        acceptance_exit_code=result_payload.get("acceptance_exit_code"),
        patch_sha256=result_payload.get("patch_sha256"),
        report_path=str(report),
        result_path=str(result_path),
    )
    result_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
