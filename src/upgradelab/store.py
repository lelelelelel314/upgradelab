"""Transactional SQLite store for run coordination and evidence."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import (
    ConcurrencyConflict,
    EffectConflict,
    EffectNeedsReconciliation,
    InvalidTransition,
    LeaseLost,
)
from .fingerprints import canonical_json_hash
from .models import (
    ALLOWED_TRANSITIONS,
    Checkpoint,
    Effect,
    EffectDecision,
    EffectStatus,
    Lease,
    Run,
    RunStatus,
    TaskSpec,
)


class SQLiteRunStore:
    """Durable run store with compare-and-swap revisions and fenced workers."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    fence_token INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_claim
                    ON runs(status, lease_expires_at, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, sequence);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    environment_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    operation_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fence_token INTEGER NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (run_id, operation_key)
                );
                """
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: float,
    ) -> None:
        connection.execute(
            "INSERT INTO events(run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, self._json(dict(payload)), now),
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            task=TaskSpec.from_dict(json.loads(row["task_json"])),
            status=RunStatus(row["status"]),
            stage=row["stage"],
            revision=row["revision"],
            fence_token=row["fence_token"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt=row["attempt"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_effect(row: sqlite3.Row) -> Effect:
        return Effect(
            run_id=row["run_id"],
            operation_key=row["operation_key"],
            name=row["name"],
            request_hash=row["request_hash"],
            status=EffectStatus(row["status"]),
            fence_token=row["fence_token"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_run(self, task: TaskSpec, *, run_id: str | None = None) -> Run:
        run_id = run_id or str(uuid.uuid4())
        now = self.clock()
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO runs(id, task_json, status, stage, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, self._json(task.to_dict()), RunStatus.PENDING, "queued", now, now),
            )
            self._append_event(connection, run_id, "RUN_CREATED", {"stage": "queued"}, now)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._row_to_run(row)

    def claim_next(self, owner: str, *, lease_seconds: float = 30.0) -> Lease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self.clock()
        expires_at = now + lease_seconds
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT * FROM runs
                   WHERE status = ?
                      OR (status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                   ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, created_at, id LIMIT 1""",
                (RunStatus.PENDING, RunStatus.RUNNING, now, RunStatus.RUNNING),
            ).fetchone()
            if row is None:
                return None
            recovering = row["status"] == RunStatus.RUNNING
            fence_token = int(row["fence_token"]) + 1
            cursor = connection.execute(
                """UPDATE runs SET status = ?, lease_owner = ?, lease_expires_at = ?,
                   fence_token = ?, attempt = attempt + 1, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (RunStatus.RUNNING, owner, expires_at, fence_token, now, row["id"], row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("claim lost a compare-and-swap race")
            self._append_event(
                connection,
                row["id"],
                "LEASE_RECOVERED" if recovering else "RUN_CLAIMED",
                {"owner": owner, "fence_token": fence_token, "previous_owner": row["lease_owner"]},
                now,
            )
        return Lease(row["id"], owner, fence_token, expires_at)

    def acquire_run(self, run_id: str, owner: str, *, lease_seconds: float = 30.0) -> Lease:
        """Acquire one known run, allowing a durable orchestrator to re-enter safely."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self.clock()
        expires_at = now + lease_seconds
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            status = RunStatus(row["status"])
            if status is RunStatus.RUNNING and row["lease_expires_at"] > now:
                if row["lease_owner"] != owner:
                    raise LeaseLost("run has a live lease owned by another worker")
                return Lease(run_id, owner, int(row["fence_token"]), float(row["lease_expires_at"]))
            if status not in {RunStatus.PENDING, RunStatus.RUNNING}:
                raise InvalidTransition(f"cannot acquire run in {status} state")

            recovering = status is RunStatus.RUNNING
            fence_token = int(row["fence_token"]) + 1
            cursor = connection.execute(
                """UPDATE runs SET status = ?, lease_owner = ?, lease_expires_at = ?,
                   fence_token = ?, attempt = attempt + 1, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (RunStatus.RUNNING, owner, expires_at, fence_token, now, run_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("acquire lost a compare-and-swap race")
            self._append_event(
                connection,
                run_id,
                "LEASE_RECOVERED" if recovering else "RUN_CLAIMED",
                {"owner": owner, "fence_token": fence_token, "previous_owner": row["lease_owner"]},
                now,
            )
        return Lease(run_id, owner, fence_token, expires_at)

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        lease: Lease,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (lease.run_id,)).fetchone()
        if row is None:
            raise KeyError(lease.run_id)
        if row["lease_owner"] != lease.owner or row["fence_token"] != lease.fence_token:
            raise LeaseLost("worker lease was fenced by another owner")
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
            raise LeaseLost("worker lease expired")
        return row

    def renew_lease(self, lease: Lease, *, lease_seconds: float = 30.0) -> Lease:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self.clock()
        expires_at = now + lease_seconds
        with self._transaction() as connection:
            row = self._require_lease(connection, lease, now)
            cursor = connection.execute(
                """UPDATE runs SET lease_expires_at = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (expires_at, now, lease.run_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("lease renewal lost a compare-and-swap race")
            self._append_event(
                connection,
                lease.run_id,
                "LEASE_RENEWED",
                {"owner": lease.owner, "fence_token": lease.fence_token},
                now,
            )
        return Lease(lease.run_id, lease.owner, lease.fence_token, expires_at)

    def transition(
        self,
        lease: Lease,
        target: RunStatus,
        *,
        stage: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> Run:
        now = self.clock()
        with self._transaction() as connection:
            row = self._require_lease(connection, lease, now)
            current = RunStatus(row["status"])
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"cannot transition {current} to {target}")
            next_stage = stage or row["stage"]
            terminal = target in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}
            cursor = connection.execute(
                """UPDATE runs SET status = ?, stage = ?, result_json = ?, last_error = ?,
                   lease_owner = ?, lease_expires_at = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (
                    target,
                    next_stage,
                    self._json(dict(result)) if result is not None else row["result_json"],
                    error,
                    None if terminal else row["lease_owner"],
                    None if terminal else row["lease_expires_at"],
                    now,
                    lease.run_id,
                    row["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("transition lost a compare-and-swap race")
            self._append_event(
                connection,
                lease.run_id,
                "RUN_TRANSITIONED",
                {"from": current, "to": target, "stage": next_stage, "error": error},
                now,
            )
        return self.get_run(lease.run_id)

    def advance_stage(
        self,
        lease: Lease,
        stage: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> Run:
        now = self.clock()
        with self._transaction() as connection:
            row = self._require_lease(connection, lease, now)
            if RunStatus(row["status"]) is not RunStatus.RUNNING:
                raise InvalidTransition("stage can advance only while running")
            cursor = connection.execute(
                """UPDATE runs SET stage = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (stage, now, lease.run_id, row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("stage update lost a compare-and-swap race")
            payload: dict[str, object] = {"stage": stage}
            if evidence:
                payload["evidence"] = dict(evidence)
            self._append_event(connection, lease.run_id, "STAGE_STARTED", payload, now)
        return self.get_run(lease.run_id)

    def record_checkpoint(
        self,
        lease: Lease,
        *,
        stage: str,
        source_fingerprint: str,
        environment_fingerprint: str,
        payload: Mapping[str, Any],
    ) -> Checkpoint:
        now = self.clock()
        with self._transaction() as connection:
            self._require_lease(connection, lease, now)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints WHERE run_id = ?",
                (lease.run_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO checkpoints(
                   run_id, sequence, stage, source_fingerprint,
                   environment_fingerprint, payload_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    lease.run_id,
                    sequence,
                    stage,
                    source_fingerprint,
                    environment_fingerprint,
                    self._json(dict(payload)),
                    now,
                ),
            )
            self._append_event(
                connection,
                lease.run_id,
                "CHECKPOINT_RECORDED",
                {"sequence": sequence, "stage": stage},
                now,
            )
        return Checkpoint(
            run_id=lease.run_id,
            sequence=sequence,
            stage=stage,
            source_fingerprint=source_fingerprint,
            environment_fingerprint=environment_fingerprint,
            payload=dict(payload),
            created_at=now,
        )

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            run_id=row["run_id"],
            sequence=row["sequence"],
            stage=row["stage"],
            source_fingerprint=row["source_fingerprint"],
            environment_fingerprint=row["environment_fingerprint"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )

    def begin_effect(
        self,
        lease: Lease,
        *,
        operation_key: str,
        name: str,
        request: Mapping[str, Any],
    ) -> EffectDecision:
        now = self.clock()
        request_hash = canonical_json_hash(request)
        needs_reconciliation = False
        with self._transaction() as connection:
            self._require_lease(connection, lease, now)
            row = connection.execute(
                "SELECT * FROM effects WHERE run_id = ? AND operation_key = ?",
                (lease.run_id, operation_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO effects(
                       run_id, operation_key, name, request_hash, status,
                       fence_token, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        lease.run_id,
                        operation_key,
                        name,
                        request_hash,
                        EffectStatus.STARTED,
                        lease.fence_token,
                        now,
                        now,
                    ),
                )
                self._append_event(
                    connection,
                    lease.run_id,
                    "EFFECT_STARTED",
                    {"operation_key": operation_key, "name": name},
                    now,
                )
                row = connection.execute(
                    "SELECT * FROM effects WHERE run_id = ? AND operation_key = ?",
                    (lease.run_id, operation_key),
                ).fetchone()
                return EffectDecision(True, self._row_to_effect(row))

            effect = self._row_to_effect(row)
            if effect.name != name or effect.request_hash != request_hash:
                raise EffectConflict("operation key was reused with a different request")
            if effect.status is EffectStatus.SUCCEEDED:
                return EffectDecision(False, effect)
            if effect.status is EffectStatus.STARTED and effect.fence_token != lease.fence_token:
                connection.execute(
                    """UPDATE effects SET status = ?, updated_at = ?
                       WHERE run_id = ? AND operation_key = ? AND status = ?""",
                    (
                        EffectStatus.UNKNOWN,
                        now,
                        lease.run_id,
                        operation_key,
                        EffectStatus.STARTED,
                    ),
                )
                self._append_event(
                    connection,
                    lease.run_id,
                    "EFFECT_BECAME_UNKNOWN",
                    {"operation_key": operation_key, "previous_fence": effect.fence_token},
                    now,
                )
                needs_reconciliation = True
            elif effect.status is EffectStatus.UNKNOWN:
                needs_reconciliation = True
            elif effect.status is EffectStatus.FAILED:
                connection.execute(
                    """UPDATE effects SET status = ?, fence_token = ?, error = NULL, updated_at = ?
                       WHERE run_id = ? AND operation_key = ?""",
                    (
                        EffectStatus.STARTED,
                        lease.fence_token,
                        now,
                        lease.run_id,
                        operation_key,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM effects WHERE run_id = ? AND operation_key = ?",
                    (lease.run_id, operation_key),
                ).fetchone()
                return EffectDecision(True, self._row_to_effect(row))
            else:
                needs_reconciliation = True
        if needs_reconciliation:
            raise EffectNeedsReconciliation(operation_key)
        raise AssertionError("unreachable effect decision")

    def finish_effect(
        self,
        lease: Lease,
        *,
        operation_key: str,
        succeeded: bool,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> Effect:
        now = self.clock()
        target = EffectStatus.SUCCEEDED if succeeded else EffectStatus.FAILED
        with self._transaction() as connection:
            self._require_lease(connection, lease, now)
            cursor = connection.execute(
                """UPDATE effects SET status = ?, result_json = ?, error = ?, updated_at = ?
                   WHERE run_id = ? AND operation_key = ? AND status = ? AND fence_token = ?""",
                (
                    target,
                    self._json(dict(result)) if result is not None else None,
                    error,
                    now,
                    lease.run_id,
                    operation_key,
                    EffectStatus.STARTED,
                    lease.fence_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLost("effect is not owned by this fenced worker")
            self._append_event(
                connection,
                lease.run_id,
                "EFFECT_FINISHED",
                {"operation_key": operation_key, "status": target},
                now,
            )
            row = connection.execute(
                "SELECT * FROM effects WHERE run_id = ? AND operation_key = ?",
                (lease.run_id, operation_key),
            ).fetchone()
        return self._row_to_effect(row)

    def reconcile_effect(
        self,
        lease: Lease,
        *,
        operation_key: str,
        succeeded: bool,
        evidence: Mapping[str, Any],
    ) -> Effect:
        if not evidence:
            raise ValueError("reconciliation requires evidence")
        now = self.clock()
        target = EffectStatus.SUCCEEDED if succeeded else EffectStatus.FAILED
        with self._transaction() as connection:
            self._require_lease(connection, lease, now)
            cursor = connection.execute(
                """UPDATE effects SET status = ?, result_json = ?, error = ?,
                   fence_token = ?, updated_at = ?
                   WHERE run_id = ? AND operation_key = ? AND status = ?""",
                (
                    target,
                    self._json(dict(evidence)) if succeeded else None,
                    None if succeeded else self._json(dict(evidence)),
                    lease.fence_token,
                    now,
                    lease.run_id,
                    operation_key,
                    EffectStatus.UNKNOWN,
                ),
            )
            if cursor.rowcount != 1:
                raise EffectConflict("only UNKNOWN effects can be reconciled")
            self._append_event(
                connection,
                lease.run_id,
                "EFFECT_RECONCILED",
                {"operation_key": operation_key, "status": target, "evidence": dict(evidence)},
                now,
            )
            row = connection.execute(
                "SELECT * FROM effects WHERE run_id = ? AND operation_key = ?",
                (lease.run_id, operation_key),
            ).fetchone()
        return self._row_to_effect(row)

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
