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


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    slug: str
    directory: str
    patch: str
    touched_files: tuple[str, ...]
    rationale: str


PYDANTIC_CASES = (
    BenchmarkCase(
        slug="field-pattern",
        directory="pydantic_v2_field_pattern",
        patch=textwrap.dedent(
            """\
            diff --git a/profile.py b/profile.py
            --- a/profile.py
            +++ b/profile.py
            @@ -5 +5 @@ class Registration(BaseModel):
            -    username: str = Field(min_length=3, regex=r"^[a-z][a-z0-9_]+$")
            +    username: str = Field(min_length=3, pattern=r"^[a-z][a-z0-9_]+$")
            """
        ),
        touched_files=("profile.py",),
        rationale="Replace the removed Field regex argument with the Pydantic v2 pattern argument.",
    ),
    BenchmarkCase(
        slug="field-validator",
        directory="pydantic_v2_validator",
        patch=(
            "diff --git a/account.py b/account.py\n"
            "--- a/account.py\n"
            "+++ b/account.py\n"
            "@@ -1,9 +1,10 @@\n"
            "-from pydantic import BaseModel, validator\n"
            "+from pydantic import BaseModel, field_validator\n"
            " \n"
            " \n"
            " class Account(BaseModel):\n"
            "     email: str\n"
            " \n"
            "-    @validator(\"email\")\n"
            "+    @field_validator(\"email\")\n"
            "+    @classmethod\n"
            "     def normalize_email(cls, value: str) -> str:\n"
            "         return value.strip().lower()\n"
        ),
        touched_files=("account.py",),
        rationale="Migrate the deprecated v1 validator decorator to field_validator.",
    ),
    BenchmarkCase(
        slug="model-validate",
        directory="pydantic_v2_model_validate",
        patch=textwrap.dedent(
            """\
            diff --git a/loader.py b/loader.py
            --- a/loader.py
            +++ b/loader.py
            @@ -10 +10 @@ def load_job(payload: dict[str, object]) -> Job:
            -    return Job.parse_obj(payload)
            +    return Job.model_validate(payload)
            """
        ),
        touched_files=("loader.py",),
        rationale="Replace the deprecated parse_obj entry point with model_validate.",
    ),
)


class DeterministicMigrationRepairer:
    """Deterministic baseline used to validate benchmark and evidence plumbing."""

    def __init__(self, case: BenchmarkCase) -> None:
        self.case = case

    def propose(self, task, failure, context) -> PatchCandidate:
        missing = set(self.case.touched_files) - set(context.paths())
        if missing:
            raise RuntimeError(f"benchmark context did not select: {sorted(missing)}")
        return PatchCandidate(
            self.case.patch,
            self.case.touched_files,
            self.case.rationale,
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


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteResult:
    suite: str
    dependency_version: str
    total_cases: int
    succeeded_cases: int
    success_rate: float
    duration_seconds: float
    cases: tuple[BenchmarkResult, ...]
    result_path: str


def run_pydantic_pattern_benchmark(
    output_root: str | Path,
    *,
    python_executable: str = sys.executable,
) -> BenchmarkResult:
    """Run the original single case for API compatibility."""
    return _run_case(PYDANTIC_CASES[0], Path(output_root), python_executable)


def run_pydantic_benchmark_suite(
    output_root: str | Path,
    *,
    python_executable: str = sys.executable,
) -> BenchmarkSuiteResult:
    suite_root = Path(output_root).resolve() / f"pydantic-suite-{uuid4().hex[:8]}"
    started = time.monotonic()
    results = tuple(
        _run_case(case, suite_root, python_executable) for case in PYDANTIC_CASES
    )
    succeeded = sum(result.status == "SUCCEEDED" for result in results)
    result_path = suite_root / "suite-result.json"
    suite = BenchmarkSuiteResult(
        suite="pydantic-v2-migrations",
        dependency_version=version("pydantic"),
        total_cases=len(results),
        succeeded_cases=succeeded,
        success_rate=succeeded / len(results) if results else 0.0,
        duration_seconds=time.monotonic() - started,
        cases=results,
        result_path=str(result_path),
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(asdict(suite), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return suite


def _run_case(
    case: BenchmarkCase,
    output_root: Path,
    python_executable: str,
) -> BenchmarkResult:
    repository_root = Path(__file__).resolve().parents[2]
    case_root = repository_root / "benchmarks" / case.directory
    fixture = case_root / "fixture"
    verifier = case_root / "verifier.py"
    if not fixture.is_dir() or not verifier.is_file():
        raise RuntimeError(f"benchmark files are missing for {case.slug}")

    run_dir = output_root.resolve() / f"{case.slug}-{uuid4().hex[:8]}"
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
        DeterministicMigrationRepairer(case),
    ).execute(lease)
    duration = time.monotonic() - started
    report = write_html_report(store, run.id, run_dir / "report.html")
    stored = store.get_run(run.id)
    result_payload = stored.result or {}
    result_path = run_dir / "result.json"
    result = BenchmarkResult(
        case=f"pydantic-v2-{case.slug}",
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
    if pipeline_result.status.value != result.status:
        raise RuntimeError("pipeline and persisted benchmark status diverged")
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
