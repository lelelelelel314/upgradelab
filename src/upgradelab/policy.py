from __future__ import annotations

import fnmatch
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .errors import PatchPolicyViolation

if TYPE_CHECKING:
    from .workspace import PatchCandidate


DEFAULT_PROTECTED_PATTERNS = (
    ".git/**",
    ".github/**",
    ".circleci/**",
    ".gitlab/**",
    ".gitlab-ci.yml",
    ".upgradelab/**",
    ".travis.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "tox.ini",
    "tests/**",
    "test/**",
    "test_*.py",
    "*_test.py",
    "**/test_*.py",
    "**/*_test.py",
    "benchmarks/verifier/**",
    "benchmarks/verifiers/**",
    "benchmarks/**/verifier.py",
)


@dataclass(frozen=True, slots=True)
class PatchInspection:
    paths: tuple[str, ...]
    additions: int
    deletions: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    protected_patterns: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS
    max_files: int = 8
    max_patch_bytes: int = 200_000
    version: str = "v1"

    def inspect(self, candidate: PatchCandidate) -> PatchInspection:
        encoded_size = len(candidate.patch.encode("utf-8"))
        if encoded_size > self.max_patch_bytes:
            raise PatchPolicyViolation(
                f"patch has {encoded_size} bytes; limit is {self.max_patch_bytes}"
            )
        lowered = candidate.patch.lower()
        forbidden_markers = (
            "git binary patch",
            "binary files ",
            "new file mode 120000",
            "old mode 120000",
            "new file mode 160000",
            "old mode 160000",
        )
        if any(marker in lowered for marker in forbidden_markers):
            raise PatchPolicyViolation("binary, symlink, and submodule patches are not allowed")

        actual_paths: list[str] = []
        additions = 0
        deletions = 0
        for line in candidate.patch.splitlines():
            if line.startswith("diff --git "):
                try:
                    fields = shlex.split(line)
                except ValueError as error:
                    raise PatchPolicyViolation("malformed diff header") from error
                if len(fields) != 4:
                    raise PatchPolicyViolation("diff header must contain old and new paths")
                for raw in fields[2:]:
                    path = self._normalize_diff_path(raw)
                    if path is not None and path not in actual_paths:
                        actual_paths.append(path)
            elif line.startswith(("--- ", "+++ ")):
                try:
                    fields = shlex.split(line[4:].split("\t", 1)[0])
                except ValueError as error:
                    raise PatchPolicyViolation("malformed file path header") from error
                if not fields:
                    raise PatchPolicyViolation("empty file path header")
                path = self._normalize_diff_path(fields[0])
                if path is not None and path not in actual_paths:
                    actual_paths.append(path)
            elif line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

        if not actual_paths:
            raise PatchPolicyViolation("patch contains no diff --git file header")
        if len(actual_paths) > self.max_files:
            raise PatchPolicyViolation(
                f"patch touches {len(actual_paths)} files; limit is {self.max_files}"
            )

        declared = tuple(dict.fromkeys(self._normalize_relative(path) for path in candidate.touched_files))
        actual = tuple(actual_paths)
        if set(declared) != set(actual):
            raise PatchPolicyViolation(
                f"declared paths {sorted(declared)} do not match patch paths {sorted(actual)}"
            )
        protected = [path for path in actual if self.is_protected(path)]
        if protected:
            raise PatchPolicyViolation(f"patch modifies protected paths: {protected}")
        return PatchInspection(actual, additions, deletions, encoded_size)

    def is_protected(self, path: str) -> bool:
        normalized = self._normalize_relative(path)
        return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in self.protected_patterns)

    @classmethod
    def _normalize_diff_path(cls, raw: str) -> str | None:
        if raw == "/dev/null":
            return None
        if raw.startswith("a/") or raw.startswith("b/"):
            raw = raw[2:]
        return cls._normalize_relative(raw)

    @staticmethod
    def _normalize_relative(raw: str) -> str:
        if not raw or "\x00" in raw or "\\" in raw:
            raise PatchPolicyViolation(f"invalid repository path: {raw!r}")
        path = PurePosixPath(raw)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] in {"", ".git"}
        ):
            raise PatchPolicyViolation(f"path escapes repair boundary: {raw}")
        return path.as_posix()
