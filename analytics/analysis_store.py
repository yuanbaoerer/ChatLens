"""Transactional SQLite persistence for analysis runs and stage state."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

from analytics.analyzer import AnalysisResult, ArtifactRef
from analytics.models import TimeRange


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_RUN_STATUSES


class StageStatus(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"
    interrupted = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STAGE_STATUSES


_TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.succeeded,
    RunStatus.partial,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
})
_TERMINAL_STAGE_STATUSES = frozenset({
    StageStatus.succeeded,
    StageStatus.failed,
    StageStatus.skipped,
    StageStatus.cancelled,
    StageStatus.interrupted,
})


def _ensure_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    _ensure_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(raw: str, default: Any) -> Any:
    if not raw:
        return default
    return json.loads(raw)


@dataclass(frozen=True)
class StageSpec:
    index: int
    analyzer_id: str
    version: str
    prerequisites: tuple[str, ...]
    supported_message_types: frozenset[str]
    estimated_workload: int

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError("stage index must be a non-negative integer")
        for name in ("analyzer_id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"stage {name} must be a non-empty string")
        object.__setattr__(self, "prerequisites", tuple(self.prerequisites))
        object.__setattr__(self, "supported_message_types", frozenset(self.supported_message_types))
        if any(not isinstance(item, str) or not item for item in self.prerequisites):
            raise ValueError("stage prerequisites must contain non-empty strings")
        if any(not isinstance(item, str) or not item for item in self.supported_message_types):
            raise ValueError("stage supported_message_types must contain strings")
        if isinstance(self.estimated_workload, bool) or not isinstance(self.estimated_workload, int) or self.estimated_workload < 0:
            raise ValueError("stage estimated_workload must be a non-negative integer")


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    snapshot_id: str
    source_id: str
    account_scope: str
    group_id: str
    time_range: TimeRange
    timezone: str
    coverage: Mapping[str, Any]
    config_version: str
    model_id: str | None
    prompt_version: str | None
    created_at: datetime
    provider: str | None = None
    prompt_hash: str | None = None
    stages: tuple[StageSpec, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "snapshot_id", "source_id", "account_scope", "group_id", "timezone", "config_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("model_id", "prompt_version", "provider", "prompt_hash"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string or None")
        if not isinstance(self.time_range, TimeRange):
            raise TypeError("time_range must be a TimeRange")
        if not isinstance(self.coverage, Mapping):
            raise TypeError("coverage must be a mapping")
        _ensure_aware(self.created_at, "created_at")
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if any(not isinstance(item, str) or not item for item in self.diagnostics):
            raise ValueError("diagnostics must contain non-empty strings")
        indexes = [stage.index for stage in self.stages]
        if indexes != list(range(len(self.stages))):
            raise ValueError("stage indexes must be contiguous from zero")
        analyzer_ids = [stage.analyzer_id for stage in self.stages]
        if len(set(analyzer_ids)) != len(analyzer_ids):
            raise ValueError("stage analyzer IDs must be unique")


@dataclass(frozen=True)
class StageRecord:
    index: int
    analyzer_id: str
    version: str
    prerequisites: tuple[str, ...]
    supported_message_types: frozenset[str]
    estimated_workload: int
    status: StageStatus
    progress: float
    progress_message: str | None = None
    output: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error_type: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, StageStatus):
            raise TypeError("status must be a StageStatus")
        object.__setattr__(self, "prerequisites", tuple(self.prerequisites))
        object.__setattr__(self, "supported_message_types", frozenset(self.supported_message_types))
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if not 0.0 <= float(self.progress) <= 1.0:
            raise ValueError("stage progress must be between 0 and 1")
        if self.progress_message is not None and not isinstance(self.progress_message, str):
            raise TypeError("stage progress message must be a string or None")
        if self.started_at is not None:
            _ensure_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _ensure_aware(self.finished_at, "finished_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "analyzer_id": self.analyzer_id,
            "version": self.version,
            "prerequisites": list(self.prerequisites),
            "supported_message_types": sorted(self.supported_message_types),
            "estimated_workload": self.estimated_workload,
            "status": self.status.value,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "output": dict(self.output),
            "evidence_ids": list(self.evidence_ids),
            "diagnostics": list(self.diagnostics),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "error_type": self.error_type,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    snapshot_id: str
    source_id: str
    account_scope: str
    group_id: str
    time_range: TimeRange
    timezone: str
    coverage: Mapping[str, Any]
    config_version: str
    model_id: str | None
    prompt_version: str | None
    provider: str | None
    prompt_hash: str | None
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    diagnostics: tuple[str, ...]
    artifacts: tuple[ArtifactRef, ...]
    stages: tuple[StageRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise TypeError("status must be a RunStatus")
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "stages", tuple(self.stages))
        _ensure_aware(self.created_at, "created_at")
        if self.started_at is not None:
            _ensure_aware(self.started_at, "started_at")
        if self.finished_at is not None:
            _ensure_aware(self.finished_at, "finished_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "account_scope": self.account_scope,
            "group_id": self.group_id,
            "time_range": self.time_range.to_dict(),
            "timezone": self.timezone,
            "coverage": dict(self.coverage),
            "config_version": self.config_version,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "prompt_hash": self.prompt_hash,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "diagnostics": list(self.diagnostics),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "stages": [stage.to_dict() for stage in self.stages],
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    account_scope TEXT NOT NULL,
    group_id TEXT NOT NULL,
    time_start TEXT,
    time_end TEXT,
    timezone TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    config_version TEXT NOT NULL,
    model_id TEXT,
    prompt_version TEXT,
    provider TEXT,
    prompt_hash TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    diagnostics_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_stages (
    run_id TEXT NOT NULL,
    stage_index INTEGER NOT NULL,
    analyzer_id TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL,
    supported_types_json TEXT NOT NULL,
    estimated_workload INTEGER NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    progress_message TEXT,
    output_json TEXT NOT NULL DEFAULT '{}',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    error_type TEXT,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY (run_id, stage_index),
    UNIQUE (run_id, analyzer_id),
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stages_run ON run_stages (run_id, stage_index);
"""


class RunStore:
    """SQLite run manifest and stage store inside an explicit local workspace.

    The store persists run/stage metadata and analyzer-provided output, evidence
    references and artifacts. It never copies snapshot messages, sender
    identifiers or raw snapshot payloads.
    """

    SCHEMA_VERSION = 2
    DB_FILENAME = "analysis-runs.db"

    def __init__(self, workspace: Path | str) -> None:
        path = Path(workspace)
        if path.exists() and not path.is_dir():
            raise ValueError("analysis workspace must be a directory")
        path.mkdir(parents=True, exist_ok=True)
        self.workspace = path.resolve()
        self.db_path = self.workspace / self.DB_FILENAME
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, self.SCHEMA_VERSION):
                raise ValueError(f"unsupported analysis store schema version: {version}")
            connection.executescript(_SCHEMA)
            if version < self.SCHEMA_VERSION:
                self._add_missing_columns(connection)
                connection.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @staticmethod
    def _add_missing_columns(connection: sqlite3.Connection) -> None:
        """Apply the additive v1 -> v2 schema changes without dropping data."""
        for table, column, declaration in (
            ("runs", "provider", "TEXT"),
            ("runs", "prompt_hash", "TEXT"),
            ("run_stages", "progress_message", "TEXT"),
        ):
            existing = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_run(self, manifest: RunManifest) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, snapshot_id, source_id, account_scope, group_id,
                    time_start, time_end, timezone, coverage_json, config_version,
                    model_id, prompt_version, provider, prompt_hash, status, created_at,
                    started_at, finished_at, diagnostics_json, artifacts_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, '[]')
                """,
                (
                    manifest.run_id,
                    manifest.snapshot_id,
                    manifest.source_id,
                    manifest.account_scope,
                    manifest.group_id,
                    _iso(manifest.time_range.start),
                    _iso(manifest.time_range.end),
                    manifest.timezone,
                    _json(dict(manifest.coverage)),
                    manifest.config_version,
                    manifest.model_id,
                    manifest.prompt_version,
                    manifest.provider,
                    manifest.prompt_hash,
                    RunStatus.queued.value,
                    _iso(manifest.created_at),
                    _json(list(manifest.diagnostics)),
                ),
            )
            connection.executemany(
                """
                INSERT INTO run_stages (
                    run_id, stage_index, analyzer_id, analyzer_version,
                    prerequisites_json, supported_types_json, estimated_workload,
                    status, progress, output_json, evidence_ids_json,
                    diagnostics_json, artifacts_json, error_type,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', '[]', '[]', '[]', NULL, NULL, NULL)
                """,
                [
                    (
                        manifest.run_id,
                        stage.index,
                        stage.analyzer_id,
                        stage.version,
                        _json(list(stage.prerequisites)),
                        _json(sorted(stage.supported_message_types)),
                        stage.estimated_workload,
                        StageStatus.pending.value,
                    )
                    for stage in manifest.stages
                ],
            )
            connection.commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                return None
            stages = connection.execute(
                "SELECT * FROM run_stages WHERE run_id = ? ORDER BY stage_index",
                (run_id,),
            ).fetchall()
        return self._run_record(run, stages)

    def list_runs(self, limit: int = 50) -> tuple[RunRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            records = []
            for row in rows:
                stages = connection.execute(
                    "SELECT * FROM run_stages WHERE run_id = ? ORDER BY stage_index",
                    (row["run_id"],),
                ).fetchall()
                records.append(self._run_record(row, stages))
        return tuple(records)

    def mark_running(self, run_id: str, started_at: datetime) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = ?, started_at = ? WHERE run_id = ? AND status = ?",
                (RunStatus.running.value, _iso(started_at), run_id, RunStatus.queued.value),
            )
            return cursor.rowcount == 1

    def start_stage(self, run_id: str, analyzer_id: str, started_at: datetime) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_stages SET status = ?, started_at = ?, progress = 0, progress_message = NULL
                WHERE run_id = ? AND analyzer_id = ? AND status = ?
                  AND EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = ?)
                """,
                (
                    StageStatus.running.value,
                    _iso(started_at),
                    run_id,
                    analyzer_id,
                    StageStatus.pending.value,
                    run_id,
                    RunStatus.running.value,
                ),
            )
            return cursor.rowcount == 1

    def update_stage_progress(
        self,
        run_id: str,
        analyzer_id: str,
        progress: float,
        *,
        message: str | None = None,
    ) -> bool:
        value = float(progress)
        if not 0.0 <= value <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        if message is not None and not isinstance(message, str):
            raise TypeError("progress message must be a string or None")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_stages SET progress = ?, progress_message = COALESCE(?, progress_message)
                WHERE run_id = ? AND analyzer_id = ? AND status = ?
                  AND EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = ?)
                """,
                (
                    value,
                    message,
                    run_id,
                    analyzer_id,
                    StageStatus.running.value,
                    run_id,
                    RunStatus.running.value,
                ),
            )
            return cursor.rowcount == 1

    def complete_stage_success(self, run_id: str, analyzer_id: str, result: AnalysisResult, finished_at: datetime) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE run_stages SET
                    status = ?, progress = 1.0, output_json = ?, evidence_ids_json = ?,
                    diagnostics_json = ?, artifacts_json = ?, finished_at = ?
                WHERE run_id = ? AND analyzer_id = ? AND status = ?
                  AND EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = ?)
                """,
                (
                    StageStatus.succeeded.value,
                    _json(dict(result.output)),
                    _json(list(result.evidence_ids)),
                    _json(list(result.diagnostics)),
                    _json([artifact.to_dict() for artifact in result.artifacts]),
                    _iso(finished_at),
                    run_id,
                    analyzer_id,
                    StageStatus.running.value,
                    run_id,
                    RunStatus.running.value,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def complete_stage_failure(
        self,
        run_id: str,
        analyzer_id: str,
        *,
        error_type: str,
        diagnostics: tuple[str, ...],
        finished_at: datetime,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE run_stages SET status = ?, error_type = ?, diagnostics_json = ?, finished_at = ?
                WHERE run_id = ? AND analyzer_id = ? AND status = ?
                  AND EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = ?)
                """,
                (
                    StageStatus.failed.value,
                    str(error_type),
                    _json(list(diagnostics)),
                    _iso(finished_at),
                    run_id,
                    analyzer_id,
                    StageStatus.running.value,
                    run_id,
                    RunStatus.running.value,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def mark_stage_skipped(
        self,
        run_id: str,
        analyzer_id: str,
        *,
        diagnostics: tuple[str, ...],
        finished_at: datetime,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_stages SET status = ?, diagnostics_json = ?, finished_at = ?
                WHERE run_id = ? AND analyzer_id = ? AND status = ?
                  AND EXISTS (SELECT 1 FROM runs WHERE run_id = ? AND status = ?)
                """,
                (
                    StageStatus.skipped.value,
                    _json(list(diagnostics)),
                    _iso(finished_at),
                    run_id,
                    analyzer_id,
                    StageStatus.pending.value,
                    run_id,
                    RunStatus.running.value,
                ),
            )
            return cursor.rowcount == 1

    def cancel_run(self, run_id: str, *, now: datetime) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None or row["status"] not in (RunStatus.queued.value, RunStatus.running.value):
                connection.rollback()
                return False
            connection.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (RunStatus.cancelled.value, _iso(now), run_id),
            )
            connection.execute(
                """
                UPDATE run_stages SET status = ?, diagnostics_json = ?, finished_at = ?
                WHERE run_id = ? AND status IN (?, ?)
                """,
                (
                    StageStatus.cancelled.value,
                    _json(["run_cancelled"]),
                    _iso(now),
                    run_id,
                    StageStatus.pending.value,
                    StageStatus.running.value,
                ),
            )
            connection.commit()
            return True

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        diagnostics: tuple[str, ...],
        artifacts: tuple[ArtifactRef, ...],
        finished_at: datetime,
    ) -> bool:
        if status not in (RunStatus.succeeded, RunStatus.partial, RunStatus.failed):
            raise ValueError("finish_run requires a completed status")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT diagnostics_json FROM runs WHERE run_id = ? AND status = ?",
                (run_id, RunStatus.running.value),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            merged = list(_loads(row["diagnostics_json"], []))
            for diagnostic in diagnostics:
                if diagnostic not in merged:
                    merged.append(diagnostic)
            cursor = connection.execute(
                """
                UPDATE runs SET status = ?, finished_at = ?, diagnostics_json = ?, artifacts_json = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    status.value,
                    _iso(finished_at),
                    _json(merged),
                    _json([artifact.to_dict() for artifact in artifacts]),
                    run_id,
                    RunStatus.running.value,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def recover_interrupted(self, *, now: datetime) -> int:
        """Mark leftover queued/running records interrupted; terminal rows stay intact."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT run_id, diagnostics_json FROM runs WHERE status IN (?, ?)",
                (RunStatus.queued.value, RunStatus.running.value),
            ).fetchall()
            for row in rows:
                diagnostics = list(_loads(row["diagnostics_json"], []))
                if "recovered_interrupted" not in diagnostics:
                    diagnostics.append("recovered_interrupted")
                connection.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, diagnostics_json = ? WHERE run_id = ?",
                    (RunStatus.interrupted.value, _iso(now), _json(diagnostics), row["run_id"]),
                )
                connection.execute(
                    """
                    UPDATE run_stages SET status = ?, finished_at = ?
                    WHERE run_id = ? AND status IN (?, ?)
                    """,
                    (
                        StageStatus.interrupted.value,
                        _iso(now),
                        row["run_id"],
                        StageStatus.pending.value,
                        StageStatus.running.value,
                    ),
                )
            connection.commit()
            return len(rows)

    def _run_record(self, run: sqlite3.Row, stage_rows: list[sqlite3.Row]) -> RunRecord:
        stages = tuple(self._stage_record(row) for row in stage_rows)
        return RunRecord(
            run_id=run["run_id"],
            snapshot_id=run["snapshot_id"],
            source_id=run["source_id"],
            account_scope=run["account_scope"],
            group_id=run["group_id"],
            time_range=TimeRange(_parse_datetime(run["time_start"]), _parse_datetime(run["time_end"])),
            timezone=run["timezone"],
            coverage=_loads(run["coverage_json"], {}),
            config_version=run["config_version"],
            model_id=run["model_id"],
            prompt_version=run["prompt_version"],
            provider=run["provider"],
            prompt_hash=run["prompt_hash"],
            status=RunStatus(run["status"]),
            created_at=_parse_datetime(run["created_at"]),
            started_at=_parse_datetime(run["started_at"]),
            finished_at=_parse_datetime(run["finished_at"]),
            diagnostics=tuple(_loads(run["diagnostics_json"], [])),
            artifacts=tuple(ArtifactRef.from_dict(item) for item in _loads(run["artifacts_json"], [])),
            stages=stages,
        )

    def _stage_record(self, row: sqlite3.Row) -> StageRecord:
        return StageRecord(
            index=row["stage_index"],
            analyzer_id=row["analyzer_id"],
            version=row["analyzer_version"],
            prerequisites=tuple(_loads(row["prerequisites_json"], [])),
            supported_message_types=frozenset(_loads(row["supported_types_json"], [])),
            estimated_workload=row["estimated_workload"],
            status=StageStatus(row["status"]),
            progress=float(row["progress"]),
            progress_message=row["progress_message"],
            output=_loads(row["output_json"], {}),
            evidence_ids=tuple(_loads(row["evidence_ids_json"], [])),
            diagnostics=tuple(_loads(row["diagnostics_json"], [])),
            artifacts=tuple(ArtifactRef.from_dict(item) for item in _loads(row["artifacts_json"], [])),
            error_type=row["error_type"],
            started_at=_parse_datetime(row["started_at"]),
            finished_at=_parse_datetime(row["finished_at"]),
        )
