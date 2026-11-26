"""Immutable data models for ChatLens analytics snapshots and adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any


def _ensure_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


class MediaStatus(str, Enum):
    readable = "readable"
    missing = "missing"
    unsupported = "unsupported"
    failed = "failed"
    not_requested = "not_requested"


class MediaRole(str, Enum):
    main = "main"
    thumbnail = "thumbnail"
    unknown = "unknown"


class MediaQuality(str, Enum):
    original = "original"
    thumbnail = "thumbnail"
    unknown = "unknown"


@dataclass(frozen=True)
class TimeRange:
    """Timezone-aware half-open interval [start, end)."""

    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.start is not None:
            _ensure_aware(self.start, "TimeRange.start")
        if self.end is not None:
            _ensure_aware(self.end, "TimeRange.end")
        if self.start is not None and self.end is not None:
            if self.start.astimezone(timezone.utc) >= self.end.astimezone(timezone.utc):
                raise ValueError("TimeRange start must be strictly before end for [start, end)")

    def contains(self, instant: datetime) -> bool:
        _ensure_aware(instant, "instant")
        instant = instant.astimezone(timezone.utc)
        return (
            (self.start is None or self.start.astimezone(timezone.utc) <= instant)
            and (self.end is None or instant < self.end.astimezone(timezone.utc))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": _iso_utc(self.start),
            "end": _iso_utc(self.end),
        }


@dataclass(frozen=True)
class GroupInfo:
    source_id: str
    account_scope: str
    group_id: str
    display_name: str
    message_count_hint: int | None = None



@dataclass(frozen=True)
class MediaRef:
    media_id: str
    kind: str
    relative_path: str | None = None
    mime: str | None = None
    content_hash: str | None = None
    dimensions: tuple[int, int] | None = None
    frame_count: int | None = None
    is_animated: bool | None = None
    role: MediaRole = MediaRole.unknown
    quality: MediaQuality = MediaQuality.unknown
    status: MediaStatus = MediaStatus.missing
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.dimensions is not None:
            object.__setattr__(self, "dimensions", tuple(self.dimensions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "mime": self.mime,
            "content_hash": self.content_hash,
            "dimensions": list(self.dimensions) if self.dimensions is not None else None,
            "frame_count": self.frame_count,
            "is_animated": self.is_animated,
            "role": self.role.value,
            "quality": self.quality.value,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LinkMeta:
    url: str
    title: str = ""
    description: str = ""
    source_app: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "source_app": self.source_app,
        }


@dataclass(frozen=True)
class QuoteRef:
    target_message_id: str | None
    sender_pseudonym: str
    kind: str
    text_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_message_id": self.target_message_id,
            "sender_pseudonym": self.sender_pseudonym,
            "kind": self.kind,
            "text_excerpt": self.text_excerpt,
        }


@dataclass(frozen=True)
class ForwardChild:
    child_id: str
    sender_pseudonym: str
    sent_at: datetime | None
    kind: str
    text: str
    media_refs: tuple[MediaRef, ...] = ()
    link_meta: LinkMeta | None = None
    forward_children: tuple[ForwardChild, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.sent_at is not None:
            _ensure_aware(self.sent_at, "ForwardChild.sent_at")
        object.__setattr__(self, "media_refs", tuple(self.media_refs))
        object.__setattr__(self, "forward_children", tuple(self.forward_children))

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "sender_pseudonym": self.sender_pseudonym,
            "sent_at": _iso_utc(self.sent_at),
            "kind": self.kind,
            "text": self.text,
            "media_refs": [ref.to_dict() for ref in self.media_refs],
            "link_meta": self.link_meta.to_dict() if self.link_meta is not None else None,
            "forward_children": [child.to_dict() for child in self.forward_children],
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LocalEvidenceLocator:
    source_type: str
    source_ref: str
    shard: int | None = None
    local_id: int | None = None
    server_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "shard": self.shard,
            "local_id": self.local_id,
            "server_id": self.server_id,
        }


@dataclass(frozen=True)
class Message:
    source_id: str
    account_scope: str
    group_id: str
    message_id: str
    sender_pseudonym: str
    sent_at: datetime
    kind: str
    text: str
    media_refs: tuple[MediaRef, ...] = ()
    link_meta: LinkMeta | None = None
    forward_children: tuple[ForwardChild, ...] = ()
    forward_truncated: bool = False
    reply_to: QuoteRef | None = None
    local_evidence: LocalEvidenceLocator | None = None

    def __post_init__(self) -> None:
        _ensure_aware(self.sent_at, "Message.sent_at")
        object.__setattr__(self, "media_refs", tuple(self.media_refs))
        object.__setattr__(self, "forward_children", tuple(self.forward_children))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "account_scope": self.account_scope,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "sender_pseudonym": self.sender_pseudonym,
            "sent_at": _iso_utc(self.sent_at),
            "kind": self.kind,
            "text": self.text,
            "media_refs": [ref.to_dict() for ref in self.media_refs],
            "link_meta": self.link_meta.to_dict() if self.link_meta is not None else None,
            "forward_children": [child.to_dict() for child in self.forward_children],
            "forward_truncated": self.forward_truncated,
            "reply_to": self.reply_to.to_dict() if self.reply_to is not None else None,
            "local_evidence": self.local_evidence.to_dict() if self.local_evidence is not None else None,
        }


@dataclass(frozen=True)
class TypeCoverage:
    eligible: int = 0
    readable: int = 0
    missing: int = 0
    unsupported: int = 0
    failed: int = 0
    not_requested: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "eligible": self.eligible,
            "readable": self.readable,
            "missing": self.missing,
            "unsupported": self.unsupported,
            "failed": self.failed,
            "not_requested": self.not_requested,
        }


@dataclass(frozen=True)
class CoverageSummary:
    total_top_level: int
    forward_child_count: int
    earliest_at: datetime | None
    latest_at: datetime | None
    timezone: str
    by_kind: Mapping[str, int] = field(default_factory=dict)
    media_by_kind: Mapping[str, TypeCoverage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.earliest_at is not None:
            _ensure_aware(self.earliest_at, "CoverageSummary.earliest_at")
        if self.latest_at is not None:
            _ensure_aware(self.latest_at, "CoverageSummary.latest_at")
        object.__setattr__(self, "by_kind", MappingProxyType(dict(self.by_kind)))
        object.__setattr__(self, "media_by_kind", MappingProxyType(dict(self.media_by_kind)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_top_level": self.total_top_level,
            "forward_child_count": self.forward_child_count,
            "earliest_at": _iso_utc(self.earliest_at),
            "latest_at": _iso_utc(self.latest_at),
            "timezone": self.timezone,
            "by_kind": dict(self.by_kind),
            "media_by_kind": {k: v.to_dict() for k, v in self.media_by_kind.items()},
        }


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    source_id: str
    account_scope: str
    group_id: str
    time_range: TimeRange
    timezone: str
    messages: tuple[Message, ...]
    coverage: CoverageSummary
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "account_scope": self.account_scope,
            "group_id": self.group_id,
            "time_range": self.time_range.to_dict(),
            "timezone": self.timezone,
            "messages": [msg.to_dict() for msg in self.messages],
            "coverage": self.coverage.to_dict(),
            "diagnostics": list(self.diagnostics),
        }
