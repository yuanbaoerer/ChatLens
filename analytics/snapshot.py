"""Deterministic immutable Snapshot builder for ChatLens analytics."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from analytics.models import (
    CoverageSummary,
    ForwardChild,
    MediaRole,
    MediaStatus,
    Message,
    Snapshot,
    TimeRange,
    TypeCoverage,
)

SNAPSHOT_SCHEMA_VERSION = "cl-snapshot-v1"
MEDIA_KINDS = frozenset({"image", "emoji", "video", "voice", "file"})


def canonical_json(value: Any) -> str:
    """Serialize data deterministically for hashing and deduplication."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def make_stable_message_id(
    server_id: str | int | None = None,
    *,
    scope: tuple[str, str, str] = ("", "", ""),
    shard: int | None = None,
    local_id: int | None = None,
    sent_at: datetime | None = None,
    fallback_payload: dict[str, Any] | None = None,
) -> str:
    """Build a deterministic message ID.

    Hash the scoped server ID when present, then the local locator or content.
    """
    cleaned_server = str(server_id).strip() if server_id is not None else ""
    if cleaned_server and cleaned_server != "0":
        return f"srv:{_digest((scope, cleaned_server))}"

    instant = sent_at.astimezone(timezone.utc).isoformat() if sent_at is not None else None
    if local_id is not None and local_id != 0:
        return f"local:{_digest((scope, shard, local_id, instant))}"

    payload = fallback_payload if fallback_payload is not None else {
        "shard": shard,
        "local_id": local_id,
        "sent_at": instant,
    }
    return f"fallback:{_digest((scope, payload))}"


def _message_content_fingerprint(msg: Message) -> str:
    """Canonical SHA-256 of normalized message content (excluding message_id/locator)."""
    payload = msg.to_dict()
    del payload["message_id"], payload["local_evidence"]
    return _digest(payload)


def _evidence_key(message: Message) -> str:
    locator = message.local_evidence
    return canonical_json(locator.to_dict()) if locator is not None else "null"


def _with_message_id(message: Message, message_id: str) -> Message:
    old_id = message.message_id
    if message_id == old_id:
        return message

    def rebase(value: str, separator: str) -> str:
        return message_id + value[len(old_id):] if value.startswith(old_id + separator) else value

    def media_refs(refs, separator: str):
        return tuple(replace(ref, media_id=rebase(ref.media_id, separator)) for ref in refs)

    def forward(child: ForwardChild) -> ForwardChild:
        return replace(
            child,
            child_id=rebase(child.child_id, "/f"),
            media_refs=media_refs(child.media_refs, "/f"),
            forward_children=tuple(forward(nested) for nested in child.forward_children),
        )

    return replace(
        message,
        message_id=message_id,
        media_refs=media_refs(message.media_refs, ":"),
        forward_children=tuple(forward(child) for child in message.forward_children),
    )


def _count_forward_children(children: tuple[ForwardChild, ...]) -> int:
    total = 0
    for child in children:
        total += 1 + _count_forward_children(child.forward_children)
    return total


def _primary_media_status(msg: Message) -> MediaStatus:
    main_refs = [ref for ref in msg.media_refs if ref.role == MediaRole.main]
    candidates = main_refs if main_refs else list(msg.media_refs)
    if not candidates:
        return MediaStatus.missing
    if any(ref.status == MediaStatus.readable for ref in candidates):
        return MediaStatus.readable
    return candidates[0].status


def _compute_coverage(messages: tuple[Message, ...], timezone_name: str) -> CoverageSummary:
    by_kind_counts: dict[str, int] = {}
    media_buckets: dict[str, dict[str, int]] = {}
    forward_child_count = 0

    for msg in messages:
        by_kind_counts[msg.kind] = by_kind_counts.get(msg.kind, 0) + 1
        forward_child_count += _count_forward_children(msg.forward_children)

        is_media_message = msg.kind in MEDIA_KINDS or any(
            ref.role == MediaRole.main for ref in msg.media_refs
        )
        if is_media_message:
            bucket = media_buckets.setdefault(
                msg.kind,
                {
                    "eligible": 0,
                    "readable": 0,
                    "missing": 0,
                    "unsupported": 0,
                    "failed": 0,
                    "not_requested": 0,
                },
            )
            bucket["eligible"] += 1
            primary_status = _primary_media_status(msg)
            bucket[primary_status.value] += 1

    by_kind = {k: by_kind_counts[k] for k in sorted(by_kind_counts)}
    media_by_kind = {
        k: TypeCoverage(**media_buckets[k]) for k in sorted(media_buckets)
    }

    return CoverageSummary(
        total_top_level=len(messages),
        forward_child_count=forward_child_count,
        earliest_at=messages[0].sent_at if messages else None,
        latest_at=messages[-1].sent_at if messages else None,
        timezone=timezone_name,
        by_kind=by_kind,
        media_by_kind=media_by_kind,
    )


class SnapshotBuilder:
    """Builds an immutable, deterministic, idempotently deduplicated Snapshot."""

    def __init__(
        self,
        *,
        source_id: str,
        account_scope: str,
        group_id: str,
        time_range: TimeRange | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.source_id = source_id
        self.account_scope = account_scope
        self.group_id = group_id
        self.time_range = time_range if time_range is not None else TimeRange()
        self.timezone_name = timezone_name
        # base_message_id -> {content_fingerprint -> Message}
        self._buckets: dict[str, dict[str, Message]] = {}
        self._diagnostics: list[str] = []

    def add_diagnostic(self, diagnostic: str) -> None:
        if diagnostic:
            self._diagnostics.append(diagnostic)

    def add_message(self, message: Message) -> bool:
        if not self.time_range.contains(message.sent_at):
            return False
        scope = (message.source_id, message.account_scope, message.group_id)
        if scope != (self.source_id, self.account_scope, self.group_id):
            raise ValueError("message does not belong to the snapshot scope")

        base_id = message.message_id.strip()
        if not base_id:
            ev = message.local_evidence
            base_id = make_stable_message_id(
                server_id=ev.server_id if ev else None,
                scope=scope,
                shard=ev.shard if ev else None,
                local_id=ev.local_id if ev else None,
                sent_at=message.sent_at,
                fallback_payload={
                    "sender_pseudonym": message.sender_pseudonym,
                    "sent_at": message.sent_at.astimezone(timezone.utc).isoformat(),
                    "kind": message.kind,
                    "text": message.text,
                },
            )
            message = replace(message, message_id=base_id)

        fingerprint = _message_content_fingerprint(message)
        variants = self._buckets.setdefault(base_id, {})
        previous = variants.get(fingerprint)
        if previous is None or _evidence_key(message) < _evidence_key(previous):
            variants[fingerprint] = message
        return True

    def add_messages(self, messages: Iterable[Message]) -> None:
        for message in messages:
            self.add_message(message)

    def build(self) -> Snapshot:
        resolved: list[Message] = []
        used_ids = set(self._buckets)
        diagnostics = list(self._diagnostics)

        for base_id in sorted(self._buckets):
            variants = self._buckets[base_id]
            if len(variants) > 1:
                diagnostics.append(f"message_id_collision:{base_id}:variants={len(variants)}")
            ordered_variants = sorted(
                variants.items(),
                key=lambda item: (item[1].sent_at.astimezone(timezone.utc), item[0]),
            )
            for index, (fingerprint, message) in enumerate(ordered_variants):
                final_id = base_id
                if index:
                    final_id = self._unique_collision_id(base_id, fingerprint, used_ids)
                    used_ids.add(final_id)
                resolved.append(_with_message_id(message, final_id))

        resolved.sort(key=lambda msg: (msg.sent_at.astimezone(timezone.utc), msg.message_id))
        frozen_messages = tuple(resolved)
        frozen_diagnostics = tuple(sorted(set(diagnostics)))
        coverage = _compute_coverage(frozen_messages, self.timezone_name)

        snapshot_payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source_id": self.source_id,
            "account_scope": self.account_scope,
            "group_id": self.group_id,
            "time_range": self.time_range.to_dict(),
            "timezone": self.timezone_name,
            "messages": [msg.to_dict() for msg in frozen_messages],
            "coverage": coverage.to_dict(),
            "diagnostics": list(frozen_diagnostics),
        }
        snapshot_id = _digest(snapshot_payload)

        return Snapshot(
            snapshot_id=snapshot_id,
            source_id=self.source_id,
            account_scope=self.account_scope,
            group_id=self.group_id,
            time_range=self.time_range,
            timezone=self.timezone_name,
            messages=frozen_messages,
            coverage=coverage,
            diagnostics=frozen_diagnostics,
        )

    @staticmethod
    def _unique_collision_id(base_id: str, fingerprint: str, used_ids: set[str]) -> str:
        for length in (8, 12, 16, 24, 32, len(fingerprint)):
            candidate = f"{base_id}#c_{fingerprint[:length]}"
            if candidate not in used_ids:
                return candidate
        counter = 1
        while f"{base_id}#c_{fingerprint}_{counter}" in used_ids:
            counter += 1
        return f"{base_id}#c_{fingerprint}_{counter}"
