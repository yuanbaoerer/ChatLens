"""Read-only cross-platform SQLite archive adapter built on core.data.Archive.

Strictly avoids importing Windows-only or resource-downloading modules
(`core.resources`, `core.service`, `wx4.media`).
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from core.data import Archive
from wx4 import WECHAT_TIME_ZONE
from wx4.forward import build_forward

from analytics.adapters._moect import link_meta, quote_ref
from analytics.models import (
    ForwardChild,
    GroupInfo,
    LocalEvidenceLocator,
    MediaQuality,
    MediaRef,
    MediaRole,
    MediaStatus,
    Message,
    MessageCursor,
    MessagePage,
    Snapshot,
    TimeRange,
)
from analytics.privacy import Pseudonymizer
from analytics.snapshot import MEDIA_KINDS, SnapshotBuilder, make_stable_message_id

OFFLINE_MEDIA_REASON = "offline_sqlite_without_media_store"
TIMEZONE_NAME = WECHAT_TIME_ZONE.tzname(None) or "Asia/Shanghai"
# Positive Unix seconds with room for conversion to Beijing time.
MAX_TIMESTAMP = 253402271999


def _message_datetime(value: Any) -> datetime | None:
    """Parse a stored create_time without letting one damaged row abort a read."""
    try:
        seconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return datetime.fromtimestamp(seconds, WECHAT_TIME_ZONE)
    except (OSError, OverflowError, ValueError):
        return None


def _offline_media_refs(owner_id: str, kind: str) -> tuple[MediaRef, ...]:
    if kind not in MEDIA_KINDS:
        return ()
    return (
        MediaRef(
            media_id=f"{owner_id}:m0",
            kind=kind,
            relative_path=None,
            role=MediaRole.main,
            quality=MediaQuality.unknown,
            status=MediaStatus.not_requested,
            reason=OFFLINE_MEDIA_REASON,
        ),
    )


class SqliteArchiveAdapter:
    """Offline, strictly read-only adapter over decrypted WeChat 4.x SQLite shards."""

    def __init__(
        self,
        library: dict[str, Any],
        *,
        source_id: str | None = None,
        scope_salt: str | None = None,
        max_forward_depth: int = 6,
        max_forward_children: int = 5000,
    ) -> None:
        if not isinstance(library, dict) or not library.get("root"):
            raise ValueError("library must be a dict containing a non-empty 'root' path")
        self._library = dict(library)
        self.max_forward_depth = min(6, max(1, int(max_forward_depth)))
        self.max_forward_children = min(5000, max(1, int(max_forward_children)))
        self._explicit_scope_salt = scope_salt
        resolved_root = str(Path(self._library["root"]).resolve(strict=False))
        if source_id is not None:
            self.source_id = source_id
        else:
            root_digest = hashlib.sha256(resolved_root.encode("utf-8")).hexdigest()[:12]
            self.source_id = f"sqlite:{root_digest}"

        raw_wxid = str(self._library.get("wxid") or "").strip()
        if raw_wxid:
            acct_digest = hashlib.sha256(
                f"{self.source_id}:{raw_wxid}".encode("utf-8")
            ).hexdigest()[:12]
            self.account_scope = f"acct_{acct_digest}"
        else:
            self.account_scope = "acct_unknown"

        self._archive: Archive | None = None

    def _get_archive(self) -> Archive:
        if self._archive is None:
            self._archive = Archive(self._library)
        return self._archive

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None

    def __enter__(self) -> SqliteArchiveAdapter:
        self._get_archive()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _pseudonymizer_for(self, group_id: str) -> Pseudonymizer:
        salt = (
            f"{self._explicit_scope_salt or ''}:{self.source_id}:{self.account_scope}:{group_id}"
        )
        return Pseudonymizer(salt)

    def list_groups(self) -> list[GroupInfo]:
        archive = self._get_archive()
        groups: list[GroupInfo] = []
        for row in archive.sessions():
            username = str(row.get("username") or "")
            if not row.get("is_group"):
                continue
            count_hint = archive.message_count(username, {})
            groups.append(
                GroupInfo(
                    source_id=self.source_id,
                    account_scope=self.account_scope,
                    group_id=username,
                    display_name=str(row.get("display") or username),
                    message_count_hint=count_hint,
                )
            )
        groups.sort(key=lambda info: info.group_id)
        return groups

    def _convert_forward_record(
        self,
        record: Any,
        *,
        parent_id: str,
        parent_sent_at: datetime,
        pseudonymizer: Pseudonymizer,
        scope: tuple[str, str, str],
        depth: int,
        budget: list[int],
    ) -> tuple[tuple[ForwardChild, ...], bool]:
        if not isinstance(record, dict):
            return (), False
        raw_children = record.get("messages")
        truncated = bool(record.get("truncated", False) or record.get("notice"))
        if not isinstance(raw_children, list) or not raw_children:
            return (), truncated

        if depth >= self.max_forward_depth:
            return (), True

        children: list[ForwardChild] = []
        for idx, item in enumerate(raw_children):
            if budget[0] <= 0:
                truncated = True
                break
            if not isinstance(item, dict):
                continue
            budget[0] -= 1

            fwd_hash = str(item.get("_forward_id") or "").strip()
            child_id = f"{parent_id}/f{idx}:{fwd_hash[:8]}" if fwd_hash else f"{parent_id}/f{idx}"
            child_kind = str(item.get("kind") or "other").strip() or "other"
            try:
                ts = int(item.get("create_time") or 0)
                child_sent_at = (
                    datetime.fromtimestamp(ts, WECHAT_TIME_ZONE) if ts > 0 else parent_sent_at
                )
            except (TypeError, ValueError, OSError, OverflowError):
                child_sent_at = parent_sent_at

            child_sender = pseudonymizer.pseudonymize(
                item.get("sender_wxid"),
                display_name=item.get("sender"),
                is_system=(child_kind == "system"),
            )
            child_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            nested_children, nested_truncated = self._convert_forward_record(
                item.get("forward"),
                parent_id=child_id,
                parent_sent_at=child_sent_at,
                pseudonymizer=pseudonymizer,
                scope=scope,
                depth=depth + 1,
                budget=budget,
            )
            children.append(
                ForwardChild(
                    child_id=child_id,
                    sender_pseudonym=child_sender,
                    sent_at=child_sent_at,
                    kind=child_kind,
                    text=str(item.get("text") or ""),
                    media_refs=_offline_media_refs(child_id, child_kind),
                    link_meta=link_meta(child_kind, child_extra),
                    forward_children=nested_children,
                    reply_to=quote_ref(item, pseudonymizer, scope),
                    truncated=nested_truncated,
                )
            )
            truncated = truncated or nested_truncated
        return tuple(children), truncated

    def _normalize_message(
        self,
        archive: Archive,
        raw: dict[str, Any],
        *,
        group_id: str,
        sent_at: datetime,
        pseudonymizer: Pseudonymizer,
    ) -> Message:
        kind = str(raw.get("kind") or "other").strip() or "other"

        sender_pseudonym = pseudonymizer.pseudonymize(
            raw.get("sender_wxid"),
            display_name=raw.get("sender"),
            is_system=(kind == "system"),
        )
        raw_server_id = str(raw.get("server_id") or "").strip()
        raw_shard = raw.get("shard")
        shard = int(raw_shard) if isinstance(raw_shard, int) and not isinstance(raw_shard, bool) else None
        raw_local_id = raw.get("local_id")
        local_id = (
            int(raw_local_id)
            if isinstance(raw_local_id, int) and not isinstance(raw_local_id, bool)
            else None
        )
        text = str(raw.get("text") or "")
        scope = (self.source_id, self.account_scope, group_id)

        message_id = make_stable_message_id(
            server_id=raw_server_id,
            scope=scope,
            shard=shard,
            local_id=local_id,
            sent_at=sent_at,
            fallback_payload={
                "group_id": group_id,
                "sender_pseudonym": sender_pseudonym,
                "sent_at": sent_at.astimezone(timezone.utc).isoformat(),
                "kind": kind,
                "text": text,
            },
        )
        extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
        media_refs = _offline_media_refs(message_id, kind)
        link = link_meta(kind, extra)
        reply_to = quote_ref(raw, pseudonymizer, scope)

        forward_children: tuple[ForwardChild, ...] = ()
        forward_truncated = False
        if kind == "chatrecord":
            record = build_forward(
                archive.data,
                raw,
                limit=self.max_forward_children,
            )
            forward_children, forward_truncated = self._convert_forward_record(
                record,
                parent_id=message_id,
                parent_sent_at=sent_at,
                pseudonymizer=pseudonymizer,
                scope=scope,
                depth=0,
                budget=[self.max_forward_children],
            )

        locator = LocalEvidenceLocator(
            source_type="sqlite",
            source_ref=self.source_id,
            shard=shard,
            local_id=local_id,
            server_id=raw_server_id or None,
        )
        return Message(
            source_id=self.source_id,
            account_scope=self.account_scope,
            group_id=group_id,
            message_id=message_id,
            sender_pseudonym=sender_pseudonym,
            sent_at=sent_at,
            kind=kind,
            text=text,
            media_refs=media_refs,
            link_meta=link,
            forward_children=forward_children,
            forward_truncated=forward_truncated,
            reply_to=reply_to,
            local_evidence=locator,
        )

    def _iter_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
        *,
        on_diagnostic: Callable[[str], None] | None = None,
    ) -> Iterator[Message]:
        archive = self._get_archive()
        if not any(row["username"] == group_id and row.get("is_group") for row in archive.sessions()):
            raise KeyError("group not found in SQLite archive")

        # Bound the SQL input before heapq merges shards. SQLite can store text
        # in an integer column and Python cannot compare it with a timestamp.
        valid_bounds = {"start": 1, "end": MAX_TIMESTAMP}
        options = dict(valid_bounds)
        if time_range is not None:
            if time_range.start is not None:
                options["start"] = max(options["start"], math.floor(time_range.start.timestamp()))
            if time_range.end is not None:
                # core.data.time_bounds uses closed [start, end]; query coarsely
                # and strictly enforce half-open [start, end) below.
                options["end"] = min(options["end"], math.ceil(time_range.end.timestamp()))

        pseudonymizer = self._pseudonymizer_for(group_id)
        invalid_timestamps = 0
        if on_diagnostic is not None:
            invalid_timestamps = archive.message_count(group_id, {}) - archive.message_count(group_id, valid_bounds)
        for raw in archive.messages(group_id, options=options, descending=False):
            sent_at = _message_datetime(raw.get("create_time"))
            if sent_at is None:
                invalid_timestamps += 1
                continue
            if time_range is not None and not time_range.contains(sent_at):
                continue
            yield self._normalize_message(
                archive,
                raw,
                group_id=group_id,
                sent_at=sent_at,
                pseudonymizer=pseudonymizer,
            )
        if invalid_timestamps and on_diagnostic is not None:
            on_diagnostic(f"sqlite_invalid_timestamp_rows:{invalid_timestamps}")

    def iter_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Iterator[Message]:
        return self._iter_messages(group_id, time_range=time_range)

    def build_snapshot(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Snapshot:
        builder = SnapshotBuilder(
            source_id=self.source_id,
            account_scope=self.account_scope,
            group_id=group_id,
            time_range=time_range,
            timezone_name=TIMEZONE_NAME,
        )
        builder.add_diagnostic("sqlite_offline_read_only_mode")
        builder.add_messages(
            self._iter_messages(
                group_id,
                time_range=time_range,
                on_diagnostic=builder.add_diagnostic,
            )
        )
        return builder.build()

    def page_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
        *,
        limit: int = 100,
        after: MessageCursor | None = None,
    ) -> MessagePage:
        return self.build_snapshot(group_id, time_range=time_range).page_messages(
            limit=limit,
            after=after,
        )
