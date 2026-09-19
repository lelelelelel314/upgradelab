from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .benchmark import run_pydantic_benchmark_suite
from .models import TaskSpec
from .pipeline import RepairPipeline
from .repairers import SubprocessRepairer
from .report import write_html_report
from .store import SQLiteRunStore
from .workspace import LocalGitWorkspace, PatchCandidate


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="upgradelab")
    parser.add_argument("--db", default=".upgradelab/runs.db", help="domain SQLite database")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="enqueue a dependency repair run")
    submit.add_argument("repo")
    submit.add_argument("dependency")
    submit.add_argument("version")
    submit.add_argument("test_command", nargs=argparse.REMAINDER)

    worker = sub.add_parser("worker", help="process one run through a JSON repairer command")
    worker.add_argument("--owner", default="local-worker")
    worker.add_argument("repairer_command", nargs=argparse.REMAINDER)

    show = sub.add_parser("show", help="show one run")
    show.add_argument("run_id")
    events = sub.add_parser("events", help="show the append-only event timeline")
    events.add_argument("run_id")
    report = sub.add_parser("report", help="write a standalone HTML evidence report")
    report.add_argument("run_id")
    report.add_argument("--output", required=True)
    demo = sub.add_parser("demo", help="run a deterministic end-to-end fixture")
    demo.add_argument("--output-dir", default=".upgradelab/demos")
    benchmark = sub.add_parser(
        "benchmark-pydantic",
        help="run the Pydantic v2 field-pattern repair benchmark",
    )
    benchmark.add_argument("--output-dir", default=".upgradelab/benchmarks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = SQLiteRunStore(args.db)
    if args.command == "submit":
        if not args.test_command:
            raise SystemExit("test_command is required after version")
        workspace = LocalGitWorkspace(args.repo)
        run = store.create_run(
            TaskSpec(
                repo_path=str(workspace.root),
                base_commit=workspace.git_head(),
                target_dependency=args.dependency,
                target_version=args.version,
                test_command=tuple(args.test_command),
            )
        )
        _print_json(asdict(run))
        return 0
    if args.command == "worker":
        if not args.repairer_command:
            raise SystemExit("repairer_command is required")
        lease = store.claim_next(args.owner, lease_seconds=300)
        if lease is None:
            _print_json({"status": "idle"})
            return 0
        run = store.get_run(lease.run_id)
        workspace = LocalGitWorkspace(run.task.repo_path)
        result = RepairPipeline(
            store,
            workspace,
            SubprocessRepairer(tuple(args.repairer_command), workspace.root),
        ).execute(lease)
        _print_json(asdict(result))
        return 0 if result.status.value == "SUCCEEDED" else 1
    if args.command == "show":
        _print_json(asdict(store.get_run(args.run_id)))
        return 0
    if args.command == "events":
        _print_json(store.events(args.run_id))
        return 0
    if args.command == "report":
        print(write_html_report(store, args.run_id, args.output))
        return 0
    if args.command == "demo":
        return _run_demo(Path(args.output_dir))
    if args.command == "benchmark-pydantic":
        result = run_pydantic_benchmark_suite(args.output_dir)
        _print_json(asdict(result))
        return 0 if result.succeeded_cases == result.total_cases else 1
    raise AssertionError(args.command)


class _DemoRepairer:
    def propose(self, task, failure, context) -> PatchCandidate:
        return PatchCandidate(
            patch=(
                "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n"
                "@@ -1,2 +1,2 @@\n def add(left, right):\n"
                "-    return left - right\n+    return left + right\n"
            ),
            touched_files=("calc.py",),
            rationale="repair the failing addition contract",
        )


def _run_demo(output_root: Path) -> int:
    demo_dir = output_root.resolve() / f"run-{uuid4().hex[:8]}"
    repo = demo_dir / "repo"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text(
        "def add(left, right):\n    return left - right\n", encoding="utf-8"
    )
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class ContractTest(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    for command in (
        ("git", "init"),
        ("git", "config", "user.email", "demo@upgradelab.invalid"),
        ("git", "config", "user.name", "UpgradeLab Demo"),
        ("git", "add", "calc.py", "test_calc.py"),
        ("git", "commit", "-m", "broken fixture"),
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    workspace = LocalGitWorkspace(repo)
    demo_store = SQLiteRunStore(demo_dir / "runs.db")
    run = demo_store.create_run(
        TaskSpec(
            str(repo),
            workspace.git_head(),
            "fixture-lib",
            "2.0.0",
            (sys.executable, "-m", "unittest", "test_calc.py"),
        )
    )
    lease = demo_store.claim_next("demo-worker", lease_seconds=60)
    assert lease is not None
    result = RepairPipeline(demo_store, workspace, _DemoRepairer()).execute(lease)
    report = write_html_report(demo_store, run.id, demo_dir / "report.html")
    _print_json({"run_id": run.id, "status": result.status, "directory": demo_dir, "report": report})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
