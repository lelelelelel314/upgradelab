from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .context import ContextManifest
from .models import TaskSpec
from .workspace import CommandResult, PatchCandidate


class RepairerProtocolError(RuntimeError):
    pass


class SubprocessRepairer:
    """Model-agnostic JSON protocol for a local repair-agent command.

    The command receives one JSON document on stdin and must emit one JSON
    object with ``patch``, ``touched_files`` and ``rationale`` on stdout.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        workspace_root: str | Path,
        *,
        timeout_seconds: float = 180.0,
        max_output_bytes: int = 2_000_000,
    ) -> None:
        if not command:
            raise ValueError("repairer command is required")
        self.command = command
        self.root = Path(workspace_root).resolve()
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def propose(
        self,
        task: TaskSpec,
        failure: CommandResult,
        context: ContextManifest,
    ) -> PatchCandidate:
        request = {
            "schema_version": 1,
            "task": task.to_dict(),
            "failure": {
                "argv": list(failure.argv),
                "exit_code": failure.exit_code,
                "stdout": failure.stdout,
                "stderr": failure.stderr,
            },
            "context": [
                {
                    "path": item.path,
                    "reason": item.reason,
                    "content": self._read_context_file(item.path),
                }
                for item in context.files
            ],
            "response_schema": {
                "patch": "unified git diff",
                "touched_files": ["repository-relative path"],
                "rationale": "short explanation",
            },
        }
        completed = subprocess.run(
            self.command,
            cwd=self.root,
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            shell=False,
        )
        if completed.returncode != 0:
            raise RepairerProtocolError(
                f"repairer exited with {completed.returncode}: {completed.stderr[-2000:]}"
            )
        if len(completed.stdout.encode("utf-8")) > self.max_output_bytes:
            raise RepairerProtocolError("repairer response exceeded the output limit")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RepairerProtocolError("repairer stdout is not one JSON document") from error
        if not isinstance(response, dict):
            raise RepairerProtocolError("repairer response must be a JSON object")
        patch = response.get("patch")
        touched = response.get("touched_files")
        rationale = response.get("rationale")
        if not isinstance(patch, str) or not patch.startswith("diff --git "):
            raise RepairerProtocolError("patch must be a unified Git diff")
        if not isinstance(touched, list) or not touched or not all(
            isinstance(item, str) and item for item in touched
        ):
            raise RepairerProtocolError("touched_files must be a non-empty string list")
        if not isinstance(rationale, str) or not rationale.strip():
            raise RepairerProtocolError("rationale must be a non-empty string")
        return PatchCandidate(patch, tuple(touched), rationale.strip())

    def _read_context_file(self, relative: str) -> str:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise RepairerProtocolError(f"context escaped workspace: {relative}") from error
        return candidate.read_text(encoding="utf-8", errors="replace")
