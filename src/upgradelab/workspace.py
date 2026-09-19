"""Trusted-local workspace execution for the first UpgradeLab milestone.

This backend intentionally does not call itself a sandbox. It runs argument vectors without a
shell and confines file operations to a configured Git worktree. A container backend will
implement the same interface later.
"""

from __future__ import annotations

import hashlib
import locale
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import EffectNeedsReconciliation
from .fingerprints import file_set_fingerprint
from .models import Lease
from .store import SQLiteRunStore


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class PatchCandidate:
    patch: str
    touched_files: tuple[str, ...]
    rationale: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()


class LocalGitWorkspace:
    """Execute commands and patches in a trusted local Git worktree."""

    def __init__(self, root: str | Path, *, timeout_seconds: float = 120.0) -> None:
        self.root = Path(root).resolve()
        self.timeout_seconds = timeout_seconds
        if not self.root.is_dir():
            raise ValueError(f"workspace does not exist: {self.root}")
        if not (self.root / ".git").exists():
            raise ValueError(f"workspace is not a Git worktree: {self.root}")

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        if not argv or any("\x00" in item for item in argv):
            raise ValueError("command must be a non-empty argument vector")
        import time

        started = time.monotonic()
        completed = subprocess.run(
            argv,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=self.timeout_seconds,
            shell=False,
            env=self._minimal_environment(),
        )
        return CommandResult(
            argv=argv,
            exit_code=completed.returncode,
            stdout=self._decode_output(completed.stdout),
            stderr=self._decode_output(completed.stderr),
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _decode_output(raw: bytes) -> str:
        encodings = ("utf-8", locale.getpreferredencoding(False), "gb18030")
        for encoding in dict.fromkeys(encodings):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "PYTHONPATH")
        return {name: os.environ[name] for name in allowed if name in os.environ}

    def source_fingerprint(self, relative_paths: tuple[str, ...]) -> str:
        return file_set_fingerprint(self.root, list(relative_paths))

    def git_head(self) -> str:
        result = self.run(("git", "rev-parse", "HEAD"))
        if not result.succeeded:
            raise RuntimeError(result.stderr.strip() or "cannot read Git HEAD")
        return result.stdout.strip()

    def git_diff(self) -> str:
        result = self.run(("git", "diff", "--no-ext-diff", "--binary"))
        if not result.succeeded:
            raise RuntimeError(result.stderr.strip() or "cannot read Git diff")
        return result.stdout

    def apply_patch(
        self,
        store: SQLiteRunStore,
        lease: Lease,
        candidate: PatchCandidate,
        *,
        operation_key: str,
    ) -> dict[str, str | bool]:
        self._validate_paths(candidate.touched_files)
        request = {"patch_sha256": candidate.sha256, "touched_files": list(candidate.touched_files)}
        try:
            decision = store.begin_effect(
                lease,
                operation_key=operation_key,
                name="git.apply_patch",
                request=request,
            )
        except EffectNeedsReconciliation:
            if self._patch_is_already_applied(candidate):
                invalidated = self._invalidate_python_bytecode(candidate.touched_files)
                fingerprint = self.source_fingerprint(candidate.touched_files)
                effect = store.reconcile_effect(
                    lease,
                    operation_key=operation_key,
                    succeeded=True,
                    evidence={
                        "source_fingerprint": fingerprint,
                        "reconciled": True,
                        "bytecode_invalidated": invalidated,
                    },
                )
                return effect.result or {"reconciled": True}
            store.reconcile_effect(
                lease,
                operation_key=operation_key,
                succeeded=False,
                evidence={"reason": "patch not present after interrupted operation"},
            )
            raise RuntimeError("interrupted patch was not applied; effect marked failed")

        if not decision.execute:
            return decision.effect.result or {"replayed": True}

        result = self._git_apply(candidate.patch)
        if not result.succeeded:
            store.finish_effect(
                lease,
                operation_key=operation_key,
                succeeded=False,
                error=result.stderr.strip() or result.stdout.strip() or "git apply failed",
            )
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git apply failed")

        invalidated = self._invalidate_python_bytecode(candidate.touched_files)
        evidence: dict[str, str | bool] = {
            "source_fingerprint": self.source_fingerprint(candidate.touched_files),
            "patch_sha256": candidate.sha256,
            "reconciled": False,
            "bytecode_invalidated": invalidated,
        }
        store.finish_effect(
            lease,
            operation_key=operation_key,
            succeeded=True,
            result=evidence,
        )
        return evidence

    def _git_apply(self, patch: str, *, reverse: bool = False, check: bool = False) -> CommandResult:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", encoding="utf-8", delete=False, dir=self.root
        ) as handle:
            handle.write(patch)
            patch_path = Path(handle.name)
        try:
            argv = ["git", "apply"]
            if reverse:
                argv.append("--reverse")
            if check:
                argv.append("--check")
            argv.append(str(patch_path))
            return self.run(tuple(argv))
        finally:
            patch_path.unlink(missing_ok=True)

    def _patch_is_already_applied(self, candidate: PatchCandidate) -> bool:
        return self._git_apply(candidate.patch, reverse=True, check=True).succeeded

    def _invalidate_python_bytecode(self, relative_paths: tuple[str, ...]) -> bool:
        removed = False
        for relative in relative_paths:
            source = (self.root / relative).resolve()
            if source.suffix != ".py":
                continue
            cache = source.parent / "__pycache__"
            if cache.is_dir():
                for compiled in cache.glob(f"{source.stem}.*.pyc"):
                    compiled.unlink(missing_ok=True)
                    removed = True
            legacy = source.with_suffix(".pyc")
            if legacy.is_file():
                legacy.unlink()
                removed = True
        return removed

    def _validate_paths(self, relative_paths: tuple[str, ...]) -> None:
        if not relative_paths:
            raise ValueError("patch candidate must declare touched files")
        for relative in relative_paths:
            path = (self.root / relative).resolve()
            if not path.is_relative_to(self.root):
                raise ValueError(f"patch path escapes workspace: {relative}")
