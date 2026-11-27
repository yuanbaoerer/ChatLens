"""Single-worker analysis service with bounded queue, progress and cancellation."""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from analytics.analyzer import (
    AnalysisCancelled,
    AnalysisContext,
    AnalysisResult,
    Analyzer,
    AnalyzerRegistry,
    Cancellation,
    ProgressReporter,
)
from analytics.analysis_store import (
    RunManifest,
    RunRecord,
    RunStatus,
    RunStore,
    StageSpec,
    StageStatus,
)
from analytics.models import Snapshot

LOG = logging.getLogger(__name__)


class AnalysisQueueFull(RuntimeError):
    """Raised when the bounded analysis queue has no admission slot."""


class AnalysisServiceClosed(RuntimeError):
    """Raised when submitting to a closed analysis service."""


class AnalysisResultError(ValueError):
    """Raised when an analyzer result cannot be validated or persisted."""


@dataclass(frozen=True)
class _Request:
    run_id: str
    snapshot: Snapshot
    context: AnalysisContext
    plan: tuple[Analyzer, ...]
    stages: tuple[StageSpec, ...]
    cancellation: Cancellation


def _snapshot_evidence_ids(snapshot: Snapshot) -> set[str]:
    evidence: set[str] = set()

    def add_child(child) -> None:
        evidence.add(child.child_id)
        for nested in child.forward_children:
            add_child(nested)

    for message in snapshot.messages:
        evidence.add(message.message_id)
        for child in message.forward_children:
            add_child(child)
    return evidence


class AnalysisService:
    """One background worker, one bounded queue, transactional run lifecycle.

    Queued and running runs can be cancelled. A stage failure skips its
    dependents while independent stages continue. Startup recovery changes only
    leftover queued/running records to ``interrupted``.
    """

    def __init__(
        self,
        registry: AnalyzerRegistry,
        workspace: Path | str,
        *,
        queue_capacity: int = 8,
        store: RunStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(registry, AnalyzerRegistry):
            raise TypeError("registry must be an AnalyzerRegistry")
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int) or queue_capacity < 1:
            raise ValueError("queue_capacity must be a positive integer")
        self.workspace = Path(workspace).resolve()
        if store is None:
            store = RunStore(self.workspace)
        elif not isinstance(store, RunStore):
            raise TypeError("store must be a RunStore")
        elif store.workspace != self.workspace:
            raise ValueError("store workspace does not match service workspace")

        self.registry = registry
        self.store = store
        self.queue_capacity = queue_capacity
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._queue: queue.Queue[_Request] = queue.Queue(maxsize=queue_capacity)
        self._admission_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._events: dict[str, threading.Event] = {}
        self._tokens: dict[str, Cancellation] = {}

        self.recovered_runs = self.store.recover_interrupted(now=self._now())
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="chatlens-analysis",
            daemon=True,
        )
        self._worker.start()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analysis clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def submit(self, snapshot: Snapshot, context: AnalysisContext | None = None) -> str:
        """Persist a queued run and enqueue it; returns the new run ID."""
        if not isinstance(snapshot, Snapshot):
            raise TypeError("snapshot must be a Snapshot")
        if context is not None and not isinstance(context, AnalysisContext):
            raise TypeError("context must be an AnalysisContext or None")
        plan = self.registry.plan()
        context = replace(context or AnalysisContext(), workspace=self.workspace)
        now = self._now()
        run_id = uuid.uuid4().hex
        stages = tuple(
            StageSpec(
                index=index,
                analyzer_id=str(getattr(analyzer, "analyzer_id")),
                version=str(getattr(analyzer, "version")),
                prerequisites=tuple(getattr(analyzer, "prerequisites", ())),
                supported_message_types=frozenset(getattr(analyzer, "supported_message_types", frozenset())),
                estimated_workload=int(getattr(analyzer, "estimated_workload", 0)),
            )
            for index, analyzer in enumerate(plan)
        )
        manifest = RunManifest(
            run_id=run_id,
            snapshot_id=snapshot.snapshot_id,
            source_id=snapshot.source_id,
            account_scope=snapshot.account_scope,
            group_id=snapshot.group_id,
            time_range=snapshot.time_range,
            timezone=snapshot.timezone,
            coverage=snapshot.coverage.to_dict(),
            config_version=context.config_version,
            model_id=context.model_id,
            prompt_version=context.prompt_version,
            created_at=now,
            stages=stages,
        )
        request = _Request(
            run_id=run_id,
            snapshot=snapshot,
            context=context,
            plan=plan,
            stages=stages,
            cancellation=Cancellation(),
        )
        event = threading.Event()

        with self._admission_lock:
            if self._closed:
                raise AnalysisServiceClosed("analysis service is closed")
            if self._queue.full():
                raise AnalysisQueueFull(f"analysis queue is full (capacity={self.queue_capacity})")
            with self._state_lock:
                if self._closed:
                    raise AnalysisServiceClosed("analysis service is closed")
                self.store.create_run(manifest)
                self._events[run_id] = event
                self._tokens[run_id] = request.cancellation
                try:
                    self._queue.put_nowait(request)
                except queue.Full as error:
                    self._events.pop(run_id, None)
                    self._tokens.pop(run_id, None)
                    self.store.cancel_run(run_id, now=self._now())
                    raise AnalysisQueueFull(
                        f"analysis queue is full (capacity={self.queue_capacity})"
                    ) from error
        return run_id

    def get(self, run_id: str) -> RunRecord | None:
        """Return the current run record, or None when the run is unknown."""
        return self.store.get_run(run_id)

    def list_runs(self, limit: int = 50) -> tuple[RunRecord, ...]:
        """Return recent run records, newest first."""
        return self.store.list_runs(limit)

    def wait(self, run_id: str, timeout: float | None = None) -> RunRecord:
        """Wait for a terminal run state and return its record."""
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
            raise TypeError("timeout must be a number or None")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        event = self._events.get(run_id)
        if event is not None:
            completed = event.wait(timeout)
        else:
            completed = True
        record = self.store.get_run(run_id)
        if record is None:
            raise KeyError(run_id)
        if not completed and not record.status.is_terminal:
            raise TimeoutError(f"analysis run {run_id} is still {record.status.value}")
        return record

    def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running run; returns whether cancellation took effect."""
        with self._state_lock:
            record = self.store.get_run(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.status not in (RunStatus.queued, RunStatus.running):
                return False
            changed = self.store.cancel_run(run_id, now=self._now())
            if changed:
                token = self._tokens.get(run_id)
                if token is not None:
                    token.cancel()
                event = self._events.get(run_id)
                if event is not None:
                    event.set()
            return changed

    def close(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            for token in self._tokens.values():
                token.cancel()
        if wait and self._worker is not threading.current_thread():
            self._worker.join(timeout=timeout)
            self.store.recover_interrupted(now=self._now())
            for event in list(self._events.values()):
                event.set()

    def __enter__(self) -> "AnalysisService":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._execute(request)
            except Exception as error:  # pragma: no cover - defensive boundary
                LOG.error(
                    "analysis worker failed run_id=%s error=%s",
                    request.run_id,
                    type(error).__name__,
                )
                self._abort_run(request.run_id)
            finally:
                self._queue.task_done()

    def _abort_run(self, run_id: str) -> None:
        with self._state_lock:
            try:
                record = self.store.get_run(run_id)
                if record is not None and record.status is RunStatus.running:
                    for stage in record.stages:
                        if stage.status is StageStatus.running:
                            self.store.complete_stage_failure(
                                run_id,
                                stage.analyzer_id,
                                error_type="internal_error",
                                diagnostics=("internal_error",),
                                finished_at=self._now(),
                            )
                        elif stage.status is StageStatus.pending:
                            self.store.mark_stage_skipped(
                                run_id,
                                stage.analyzer_id,
                                diagnostics=("internal_error",),
                                finished_at=self._now(),
                            )
                    self.store.finish_run(
                        run_id,
                        RunStatus.failed,
                        diagnostics=("internal_error",),
                        artifacts=(),
                        finished_at=self._now(),
                    )
            except Exception:
                pass
            self._signal(run_id)

    def _execute(self, request: _Request) -> None:
        with self._state_lock:
            if not self.store.mark_running(request.run_id, self._now()):
                self._signal(request.run_id)
                return
        LOG.info(
            "analysis run started run_id=%s stages=%d",
            request.run_id,
            len(request.plan),
        )

        succeeded: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for stage, analyzer in zip(request.stages, request.plan):
            if request.cancellation.is_cancelled:
                self._signal(request.run_id)
                return
            with self._state_lock:
                current = self.store.get_run(request.run_id)
                if current is None or current.status is not RunStatus.running:
                    self._signal(request.run_id)
                    return
            missing = [dependency for dependency in stage.prerequisites if dependency not in succeeded]
            if missing:
                with self._state_lock:
                    self.store.mark_stage_skipped(
                        request.run_id,
                        stage.analyzer_id,
                        diagnostics=(f"dependency_failed:{','.join(sorted(missing))}",),
                        finished_at=self._now(),
                    )
                skipped.add(stage.analyzer_id)
                continue
            with self._state_lock:
                if not self.store.start_stage(request.run_id, stage.analyzer_id, self._now()):
                    self._signal(request.run_id)
                    return

            progress = ProgressReporter(
                self._progress_callback(request.run_id, stage.analyzer_id)
            )
            try:
                result = analyzer.run(
                    request.snapshot,
                    request.context,
                    request.cancellation,
                    progress,
                )
                self._validate_result(result, request.snapshot, request.context)
                if request.cancellation.is_cancelled:
                    raise AnalysisCancelled("analysis cancelled")
                with self._state_lock:
                    committed = self.store.complete_stage_success(
                        request.run_id,
                        stage.analyzer_id,
                        result,
                        self._now(),
                    )
                if not committed:
                    self._signal(request.run_id)
                    return
                succeeded.add(stage.analyzer_id)
            except Exception as error:
                error_type = type(error).__name__
                with self._state_lock:
                    committed = self.store.complete_stage_failure(
                        request.run_id,
                        stage.analyzer_id,
                        error_type=error_type,
                        diagnostics=(f"stage_failed:{error_type}",),
                        finished_at=self._now(),
                    )
                if not committed:
                    self._signal(request.run_id)
                    return
                failed.add(stage.analyzer_id)
                LOG.warning(
                    "analysis stage failed run_id=%s analyzer=%s error=%s",
                    request.run_id,
                    stage.analyzer_id,
                    error_type,
                )

        with self._state_lock:
            current = self.store.get_run(request.run_id)
            if current is None or current.status is not RunStatus.running:
                self._signal(request.run_id)
                return
            if not request.plan:
                status = RunStatus.succeeded
                diagnostics = ["no_analyzers"]
            elif succeeded and (failed or skipped):
                status = RunStatus.partial
                diagnostics = ["missing_stages:" + ",".join(sorted(failed | skipped))]
            elif failed or skipped:
                status = RunStatus.failed
                diagnostics = ["missing_stages:" + ",".join(sorted(failed | skipped))]
            else:
                status = RunStatus.succeeded
                diagnostics = []
            artifacts = tuple(
                artifact
                for stage_record in current.stages
                for artifact in stage_record.artifacts
            )
            finished = self.store.finish_run(
                request.run_id,
                status,
                diagnostics=tuple(diagnostics),
                artifacts=artifacts,
                finished_at=self._now(),
            )
        if not finished:
            self._signal(request.run_id)
            return
        self._signal(request.run_id)
        LOG.info(
            "analysis run finished run_id=%s status=%s",
            request.run_id,
            status.value,
        )

    def _progress_callback(self, run_id: str, analyzer_id: str):
        def report(value: float, _message: str | None) -> None:
            with self._state_lock:
                self.store.update_stage_progress(run_id, analyzer_id, value)

        return report

    def _validate_result(
        self,
        result: AnalysisResult,
        snapshot: Snapshot,
        context: AnalysisContext,
    ) -> None:
        if not isinstance(result, AnalysisResult):
            raise AnalysisResultError("analyzer result must be an AnalysisResult")
        try:
            json.dumps(
                dict(result.output),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise AnalysisResultError("analyzer output is not JSON serializable") from error
        resolvable = _snapshot_evidence_ids(snapshot)
        unknown = [evidence_id for evidence_id in result.evidence_ids if evidence_id not in resolvable]
        if unknown:
            raise AnalysisResultError(f"unknown evidence IDs: {len(unknown)}")
        root = (context.workspace or self.workspace).resolve()
        for artifact in result.artifacts:
            candidate = (root / artifact.relative_path).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise AnalysisResultError(f"artifact does not exist inside workspace: {artifact.kind}")
        for diagnostic in result.diagnostics:
            if not isinstance(diagnostic, str):
                raise AnalysisResultError("analyzer diagnostics must be strings")

    def _signal(self, run_id: str) -> None:
        event = self._events.get(run_id)
        if event is not None:
            event.set()
