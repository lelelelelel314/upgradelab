"""Domain models kept independent from persistence and OpenRath adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
)


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.WAITING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
    ),
    RunStatus.WAITING: frozenset({RunStatus.RUNNING, RunStatus.CANCELED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
}


class EffectStatus(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    repo_path: str
    base_commit: str
    target_dependency: str
    target_version: str
    test_command: tuple[str, ...]
    acceptance_command: tuple[str, ...] | None = None
    max_rounds: int = 3
    token_budget: int = 50_000

    def __post_init__(self) -> None:
        if not self.repo_path:
            raise ValueError("repo_path is required")
        if not self.base_commit:
            raise ValueError("base_commit is required")
        if not self.target_dependency or not self.target_version:
            raise ValueError("target dependency and version are required")
        if not self.test_command:
            raise ValueError("test_command must be an argument vector")
        if self.acceptance_command is not None and not self.acceptance_command:
            raise ValueError("acceptance_command must be non-empty when provided")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if self.token_budget < 1:
            raise ValueError("token_budget must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "base_commit": self.base_commit,
            "target_dependency": self.target_dependency,
            "target_version": self.target_version,
            "test_command": list(self.test_command),
            "acceptance_command": (
                list(self.acceptance_command) if self.acceptance_command is not None else None
            ),
            "max_rounds": self.max_rounds,
            "token_budget": self.token_budget,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        acceptance = data.get("acceptance_command")
        return cls(
            repo_path=str(data["repo_path"]),
            base_commit=str(data["base_commit"]),
            target_dependency=str(data["target_dependency"]),
            target_version=str(data["target_version"]),
            test_command=tuple(str(item) for item in data["test_command"]),
            acceptance_command=(
                tuple(str(item) for item in acceptance) if acceptance is not None else None
            ),
            max_rounds=int(data.get("max_rounds", 3)),
            token_budget=int(data.get("token_budget", 50_000)),
        )

    def public_dict(self) -> dict[str, Any]:
        """Task fields that may cross the repairer trust boundary."""
        payload = self.to_dict()
        payload.pop("acceptance_command", None)
        return payload


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    task: TaskSpec
    status: RunStatus
    stage: str
    revision: int
    fence_token: int
    lease_owner: str | None
    lease_expires_at: float | None
    attempt: int
    created_at: float
    updated_at: float
    result: dict[str, Any] | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    owner: str
    fence_token: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class Effect:
    run_id: str
    operation_key: str
    name: str
    request_hash: str
    status: EffectStatus
    fence_token: int
    result: dict[str, Any] | None
    error: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class EffectDecision:
    execute: bool
    effect: Effect


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: str
    sequence: int
    stage: str
    source_fingerprint: str
    environment_fingerprint: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
