"""Typed analyzer contract, execution interfaces, and static dependency registry."""
from __future__ import annotations

import heapq
import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar

from analytics.models import Snapshot


class AnalysisCancelled(Exception):
    """Raised when an analyzer observes an effective cancellation request."""


class AnalyzerRegistryError(ValueError):
    """Raised before execution when the analyzer registry is not a valid DAG."""

    def __init__(self, code: str, message: str, *, analyzer_ids: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = str(code)
        self.analyzer_ids = tuple(sorted(str(item) for item in analyzer_ids))


class Cancellation:
    """Thread-safe cooperative cancellation token passed to analyzers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def check(self) -> None:
        if self._event.is_set():
            raise AnalysisCancelled("analysis cancelled")

    raise_if_cancelled = check


@dataclass(frozen=True)
class AnalysisContext:
    """Per-run metadata and configuration supplied to an analyzer.

    ``config`` is intentionally not persisted by the run store; only its
    version and optional provider/model/prompt identifiers are recorded.
    """

    config_version: str = "cl-02-default"
    config: Mapping[str, Any] = field(default_factory=dict)
    model_id: str | None = None
    prompt_version: str | None = None
    provider: str | None = None
    prompt_hash: str | None = None
    run_id: str | None = None
    workspace: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config_version, str) or not self.config_version.strip():
            raise ValueError("config_version must be a non-empty string")
        if not isinstance(self.config, Mapping):
            raise TypeError("config must be a mapping")
        object.__setattr__(self, "config", MappingProxyType(dict(self.config)))
        for name in ("model_id", "prompt_version", "provider", "prompt_hash"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id):
            raise ValueError("run_id must be a non-empty string or None")
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace))


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to a file produced by an analyzer inside the run workspace."""

    relative_path: str
    kind: str = "file"
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.relative_path, str) or not self.relative_path.strip():
            raise ValueError("artifact relative_path must be a non-empty string")
        raw = self.relative_path.strip().replace("\\", "/")
        if raw.startswith("/") or ":" in raw.split("/", 1)[0]:
            raise ValueError("artifact relative_path must be relative")
        pure = PurePosixPath(raw)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            raise ValueError("artifact relative_path must stay inside the workspace")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("artifact kind must be a non-empty string")
        if not isinstance(self.description, str):
            raise TypeError("artifact description must be a string")
        object.__setattr__(self, "relative_path", pure.as_posix())
        object.__setattr__(self, "kind", self.kind.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            relative_path=str(value["relative_path"]),
            kind=str(value.get("kind", "file")),
            description=str(value.get("description", "")),
        )


@dataclass(frozen=True)
class AnalysisResult:
    """Structured output, evidence references, and diagnostics from one stage.

    The service persists ``output`` and ``diagnostics`` verbatim. Analyzers must
    not put raw chat text or credentials there unless the feature explicitly
    requires quoted evidence.
    """

    output: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.output, Mapping):
            raise TypeError("output must be a mapping")
        object.__setattr__(self, "output", MappingProxyType(dict(self.output)))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        for evidence_id in self.evidence_ids:
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ValueError("evidence_ids must contain non-empty strings")
        for diagnostic in self.diagnostics:
            if not isinstance(diagnostic, str):
                raise TypeError("diagnostics must contain strings")
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactRef):
                raise TypeError("artifacts must contain ArtifactRef values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": dict(self.output),
            "evidence_ids": list(self.evidence_ids),
            "diagnostics": list(self.diagnostics),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


class ProgressReporter:
    """Bounds-checked, monotonic progress callback supplied to analyzers."""

    def __init__(self, callback: Callable[[float, str | None], None]) -> None:
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        self._callback = callback
        self._lock = threading.Lock()
        self._last = 0.0

    def report(self, progress: float, message: str | None = None) -> None:
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            raise TypeError("progress must be a number")
        value = float(progress)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("progress must be finite and between 0 and 1")
        if message is not None and not isinstance(message, str):
            raise TypeError("progress message must be a string or None")
        with self._lock:
            if value < self._last:
                raise ValueError("progress must not move backwards")
            self._last = value
            self._callback(value, message)

    __call__ = report


class Analyzer(ABC):
    """Static analyzer contract. Subclasses supply metadata and ``run``.

    An empty ``supported_message_types`` means all message kinds. The
    ``estimated_workload`` is a non-negative relative cost used by callers for
    planning and is persisted with the stage specification.
    """

    analyzer_id: ClassVar[str] = ""
    version: ClassVar[str] = ""
    prerequisites: ClassVar[tuple[str, ...]] = ()
    supported_message_types: ClassVar[frozenset[str]] = frozenset()
    estimated_workload: ClassVar[int] = 1

    @abstractmethod
    def run(
        self,
        snapshot: Snapshot,
        context: AnalysisContext,
        cancellation: Cancellation,
        progress: ProgressReporter,
    ) -> AnalysisResult:
        """Run against an immutable snapshot without network or source mutation."""


def _text_attribute(analyzer: Analyzer, name: str) -> str:
    value = getattr(analyzer, name, None)
    if not isinstance(value, str) or not value or value != value.strip():
        code = "invalid_analyzer_id" if name == "analyzer_id" else f"invalid_analyzer_{name}"
        raise AnalyzerRegistryError(
            code,
            f"{name} must be a non-empty string without surrounding whitespace",
            analyzer_ids=(str(getattr(analyzer, "analyzer_id", "")),),
        )
    return value


def _normalized_prerequisites(analyzer: Analyzer, analyzer_id: str) -> tuple[str, ...]:
    value = getattr(analyzer, "prerequisites", ())
    if not isinstance(value, (tuple, list)):
        raise AnalyzerRegistryError(
            "invalid_prerequisites",
            "prerequisites must be a tuple or list of analyzer IDs",
            analyzer_ids=(analyzer_id,),
        )
    result: list[str] = []
    for dependency in value:
        if not isinstance(dependency, str) or not dependency or dependency != dependency.strip():
            raise AnalyzerRegistryError(
                "invalid_prerequisite",
                "prerequisite IDs must be non-empty strings without surrounding whitespace",
                analyzer_ids=(analyzer_id,),
            )
        if dependency in result:
            raise AnalyzerRegistryError(
                "duplicate_prerequisite",
                f"duplicate prerequisite {dependency!r}",
                analyzer_ids=(analyzer_id,),
            )
        result.append(dependency)
    return tuple(result)


class AnalyzerRegistry:
    """Immutable registry that validates duplicate IDs, unknown dependencies and cycles."""

    def __init__(self, analyzers: Iterable[Analyzer] = ()) -> None:
        items = tuple(analyzers)
        by_id: dict[str, Analyzer] = {}
        for analyzer in items:
            if not isinstance(analyzer, Analyzer):
                raise TypeError("registry entries must be Analyzer instances")
            analyzer_id = _text_attribute(analyzer, "analyzer_id")
            _text_attribute(analyzer, "version")
            if analyzer_id in by_id:
                raise AnalyzerRegistryError(
                    "duplicate_analyzer_id",
                    f"duplicate analyzer_id: {analyzer_id}",
                    analyzer_ids=(analyzer_id,),
                )
            _normalized_prerequisites(analyzer, analyzer_id)
            supported = getattr(analyzer, "supported_message_types", frozenset())
            if isinstance(supported, str):
                raise AnalyzerRegistryError(
                    "invalid_supported_message_types",
                    "supported_message_types must be an iterable of strings",
                    analyzer_ids=(analyzer_id,),
                )
            try:
                supported_types = frozenset(supported)
            except TypeError as error:
                raise AnalyzerRegistryError(
                    "invalid_supported_message_types",
                    "supported_message_types must be an iterable of strings",
                    analyzer_ids=(analyzer_id,),
                ) from error
            if any(not isinstance(item, str) or not item or item != item.strip() for item in supported_types):
                raise AnalyzerRegistryError(
                    "invalid_supported_message_types",
                    "supported_message_types must contain non-empty strings",
                    analyzer_ids=(analyzer_id,),
                )
            workload = getattr(analyzer, "estimated_workload", None)
            if isinstance(workload, bool) or not isinstance(workload, int) or workload < 0:
                raise AnalyzerRegistryError(
                    "invalid_estimated_workload",
                    "estimated_workload must be a non-negative integer",
                    analyzer_ids=(analyzer_id,),
                )
            by_id[analyzer_id] = analyzer

        unknown: list[tuple[str, str]] = []
        for analyzer_id, analyzer in by_id.items():
            for dependency in _normalized_prerequisites(analyzer, analyzer_id):
                if dependency not in by_id:
                    unknown.append((analyzer_id, dependency))
        if unknown:
            ids = sorted({analyzer_id for analyzer_id, _ in unknown})
            details = ", ".join(f"{analyzer_id}->{dependency}" for analyzer_id, dependency in sorted(unknown))
            raise AnalyzerRegistryError(
                "unknown_dependency",
                f"unknown analyzer dependency: {details}",
                analyzer_ids=ids,
            )

        self._by_id = MappingProxyType(by_id)
        self._order = self._topological_order()

    def _topological_order(self) -> tuple[Analyzer, ...]:
        indegree = {analyzer_id: 0 for analyzer_id in self._by_id}
        dependents: dict[str, list[str]] = {analyzer_id: [] for analyzer_id in self._by_id}
        for analyzer_id, analyzer in self._by_id.items():
            for dependency in _normalized_prerequisites(analyzer, analyzer_id):
                indegree[analyzer_id] += 1
                dependents[dependency].append(analyzer_id)

        ready = [analyzer_id for analyzer_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ordered: list[str] = []
        while ready:
            analyzer_id = heapq.heappop(ready)
            ordered.append(analyzer_id)
            for dependent in sorted(dependents[analyzer_id]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, dependent)

        if len(ordered) != len(self._by_id):
            cycle = sorted(analyzer_id for analyzer_id, degree in indegree.items() if degree > 0)
            raise AnalyzerRegistryError(
                "dependency_cycle",
                f"analyzer dependency cycle: {', '.join(cycle)}",
                analyzer_ids=cycle,
            )
        return tuple(self._by_id[analyzer_id] for analyzer_id in ordered)

    def plan(self) -> tuple[Analyzer, ...]:
        """Return all analyzers in deterministic dependency order."""
        return self._order

    def get(self, analyzer_id: str) -> Analyzer | None:
        return self._by_id.get(analyzer_id)

    @property
    def analyzers(self) -> tuple[Analyzer, ...]:
        return self._order

    def __iter__(self):
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)


def default_registry() -> AnalyzerRegistry:
    """Production registry: only implemented analyzers are registered.

    CL-04 and later tasks add their analyzers here. Tests supply their own
    registry with synthetic analyzers.
    """
    from analytics.analyzers.media_duplicates import MediaDuplicateAnalyzer

    return AnalyzerRegistry((MediaDuplicateAnalyzer(),))
