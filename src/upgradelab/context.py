from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


TRACEBACK_FILE = re.compile(r"File [\"']([^\"']+)[\"']")
SKIP_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


@dataclass(frozen=True, slots=True)
class ContextFile:
    path: str
    reason: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ContextManifest:
    files: tuple[ContextFile, ...]
    total_bytes: int
    truncated: bool

    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)


class FailureContextSelector:
    """Build a bounded, explainable context set from a Python failure trace."""

    def __init__(self, root: Path, *, max_files: int = 24, max_bytes: int = 160_000):
        self.root = root.resolve()
        self.max_files = max_files
        self.max_bytes = max_bytes

    def select(self, failure_text: str) -> ContextManifest:
        python_files = self._python_files()
        module_to_path = self._module_index(python_files)
        imports_by_path = {
            relative: self._imports(self.root / relative, module_to_path)
            for relative in python_files
        }

        seeds: list[str] = []
        for raw_path in TRACEBACK_FILE.findall(failure_text):
            normalized = self._relative_trace_path(raw_path)
            if normalized in python_files and normalized not in seeds:
                seeds.append(normalized)

        candidates: list[tuple[str, str]] = [(path, "traceback") for path in seeds]
        for seed in seeds:
            candidates.extend((path, f"imported by {seed}") for path in imports_by_path[seed])
            for importer, imported in imports_by_path.items():
                if seed in imported:
                    candidates.append((importer, f"imports {seed}"))

        selected: list[ContextFile] = []
        seen: set[str] = set()
        total = 0
        truncated = False
        for relative, reason in candidates:
            if relative in seen:
                continue
            seen.add(relative)
            size = (self.root / relative).stat().st_size
            if len(selected) >= self.max_files or total + size > self.max_bytes:
                truncated = True
                continue
            selected.append(ContextFile(relative, reason, size))
            total += size

        return ContextManifest(tuple(selected), total, truncated)

    def _python_files(self) -> set[str]:
        result: set[str] = set()
        for path in self.root.rglob("*.py"):
            relative = path.relative_to(self.root)
            if any(part in SKIP_PARTS for part in relative.parts):
                continue
            result.add(relative.as_posix())
        return result

    @staticmethod
    def _module_index(paths: set[str]) -> dict[str, str]:
        index: dict[str, str] = {}
        for relative in paths:
            parts = list(Path(relative).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                index[".".join(parts)] = relative
        return index

    @staticmethod
    def _imports(path: Path, module_to_path: dict[str, str]) -> tuple[str, ...]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return ()

        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)

        resolved: set[str] = set()
        for module in modules:
            parts = module.split(".")
            for length in range(len(parts), 0, -1):
                candidate = ".".join(parts[:length])
                if candidate in module_to_path:
                    resolved.add(module_to_path[candidate])
                    break
        return tuple(sorted(resolved))

    def _relative_trace_path(self, raw_path: str) -> str | None:
        path = Path(raw_path)
        candidate = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        try:
            return candidate.relative_to(self.root).as_posix()
        except ValueError:
            return None
