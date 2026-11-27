"""Offline adapter for Moect JSONL and JSON exports with local media assets."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image

from analytics.adapters._moect import link_meta, quote_ref
from analytics.media_limits import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_FRAMES,
    MAX_IMAGE_TOTAL_PIXELS,
    MAX_MEDIA_FILE_BYTES,
    SUPPORTED_IMAGE_FORMATS,
)
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
from analytics.privacy import Pseudonymizer, resolve_safe_path
from analytics.snapshot import MEDIA_KINDS, SnapshotBuilder, make_stable_message_id

DEFAULT_TIMEZONE_NAME = "Asia/Shanghai"
DEFAULT_WECHAT_TZ = timezone(timedelta(hours=8), DEFAULT_TIMEZONE_NAME)
_RESOURCE_STATES = {
    "available": MediaStatus.readable,
    "missing": MediaStatus.missing,
    "encrypted": MediaStatus.unsupported,
    "unsupported": MediaStatus.unsupported,
    "corrupt": MediaStatus.failed,
    "skipped": MediaStatus.not_requested,
}
_REPORT_REASONS = frozenset({
    "aes_key_unavailable", "xor_unresolved", "encrypted_media_unresolved", "image_invalid",
    "invalid_dat_layout", "invalid_audio_size", "invalid_silk_packets", "silk_decoder_unavailable",
    "silk_decode_failed", "audio_conversion_failed",
})


def _resolve_timezone(tz_name: str | None) -> tuple[tzinfo, str]:
    name = str(tz_name or "").strip() or DEFAULT_TIMEZONE_NAME
    if name in ("Asia/Shanghai", "UTC+8", "UTC+08:00", "+08:00", "CST"):
        return DEFAULT_WECHAT_TZ, DEFAULT_TIMEZONE_NAME
    if name in ("UTC", "Z", "+00:00"):
        return timezone.utc, "UTC"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_WECHAT_TZ, DEFAULT_TIMEZONE_NAME


def _parse_timestamp(raw: dict[str, Any], tz: tzinfo, fallback: datetime | None = None) -> datetime:
    create_time = raw.get("create_time")
    if create_time is not None and create_time != "":
        try:
            ts = int(create_time)
            if ts > 10**12:
                ts //= 1000
            if 0 < ts < 253402300800:
                return datetime.fromtimestamp(ts, tz)
        except (TypeError, ValueError, OSError, OverflowError):
            pass

    time_str = str(raw.get("time_str") or "").strip()
    if time_str:
        try:
            parsed = datetime.fromisoformat(time_str)
            return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
        except (ValueError, OverflowError):
            pass

    if fallback is not None:
        return fallback
    return datetime.fromtimestamp(0, tz)


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _map_role(raw_role: Any) -> MediaRole:
    value = str(raw_role or "").strip().lower()
    if value == "main":
        return MediaRole.main
    if value in ("thumbnail", "thumb", "source_icon"):
        return MediaRole.thumbnail
    return MediaRole.unknown


def _map_quality(raw_quality: Any, role: MediaRole, raw_status: str) -> MediaQuality:
    # A thumbnail role must never be misreported as an original.
    if role == MediaRole.thumbnail:
        return MediaQuality.thumbnail
    value = str(raw_quality or "").strip().lower()
    if value == "thumbnail":
        return MediaQuality.thumbnail
    if value == "original":
        return MediaQuality.original
    if role == MediaRole.main and raw_status == "available":
        return MediaQuality.original
    return MediaQuality.unknown


def _map_non_available_status(raw_status: str, diag_reason: str | None) -> tuple[MediaStatus, str]:
    status = _RESOURCE_STATES.get(raw_status, MediaStatus.failed)
    return status, diag_reason or (raw_status if raw_status in _RESOURCE_STATES else "unknown_status")




class JsonlExportAdapter:
    """Adapter for Moect `.jsonl` and `.json` exports with local asset inspection."""

    def __init__(
        self,
        export_path: Path | str,
        approved_root: Path | None = None,
        *,
        source_id: str | None = None,
        account_scope: str | None = None,
        scope_salt: str | None = None,
        max_forward_depth: int = 6,
        max_forward_children: int = 5000,
    ) -> None:
        self.export_path = Path(export_path)
        self.approved_root = (
            Path(approved_root).resolve(strict=False)
            if approved_root is not None
            else self.export_path.parent.resolve(strict=False)
        )
        self.export_path = self.export_path.resolve()
        if not self.export_path.is_relative_to(self.approved_root):
            raise ValueError("export_path must reside inside approved_root")
        if self.export_path.suffix.lower() not in (".json", ".jsonl"):
            raise ValueError("expected a .json or .jsonl export")

        self.max_forward_depth = min(6, max(1, int(max_forward_depth)))
        self.max_forward_children = min(5000, max(1, int(max_forward_children)))
        self._explicit_scope_salt = scope_salt

        if source_id is not None:
            self.source_id = source_id
        else:
            digest = hashlib.sha256(str(self.export_path).encode("utf-8")).hexdigest()[:24]
            self.source_id = f"jsonl:{digest}"

        self.account_scope = account_scope or "export_local"

    def _source_type(self) -> str:
        return "json" if self.export_path.suffix.lower() == ".json" else "jsonl"

    def _source_ref(self) -> str:
        return self.export_path.name

    def _read_export(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        diagnostics: list[str] = []
        metadata: dict[str, Any] = {}
        records: list[dict[str, Any]] = []

        if self.export_path.suffix.lower() == ".json":
            data = json.loads(self.export_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("JSON export root must be an object")
            if isinstance(data.get("metadata"), dict):
                metadata = data["metadata"]
            raw_messages = data.get("messages")
            if isinstance(raw_messages, list):
                for idx, item in enumerate(raw_messages):
                    if isinstance(item, dict):
                        records.append(item)
                    else:
                        diagnostics.append(f"export_invalid_record:index={idx}")
            return metadata, records, diagnostics

        with self.export_path.open("r", encoding="utf-8-sig") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    diagnostics.append(f"export_malformed_jsonl:line={line_no}")
                    continue
                if not isinstance(item, dict):
                    diagnostics.append(f"export_non_object_line:line={line_no}")
                    continue
                if (
                    line_no == 1
                    and "metadata" in item
                    and isinstance(item["metadata"], dict)
                    and "kind" not in item
                    and "create_time" not in item
                ):
                    metadata = item["metadata"]
                    continue
                records.append(item)
        return metadata, records, diagnostics

    def _load_resource_report(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        diagnostics: list[str] = []
        by_server_id: dict[str, dict[str, Any]] = {}
        ambiguous_ids: set[str] = set()

        resolved_export = self.export_path.resolve(strict=False)
        export_rel_dir = resolved_export.parent.relative_to(self.approved_root)
        report_rel = (
            export_rel_dir / f"{self.export_path.stem}_assets" / "resource-report.jsonl"
        ).as_posix()
        safe_report = resolve_safe_path(self.approved_root, report_rel)
        if safe_report is None:
            diagnostics.append("resource_report_path_outside_root")
            return by_server_id, diagnostics
        try:
            if not safe_report.is_file():
                return by_server_id, diagnostics
            handle = safe_report.open("r", encoding="utf-8-sig")
        except (OSError, UnicodeError):
            # The report is optional enrichment: a permissions/encoding/IO failure must
            # not abort the export read, and the diagnostic must not leak local paths.
            diagnostics.append("resource_report_read_error")
            return by_server_id, diagnostics

        entry_count = 0
        status_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        try:
            with handle:
                for line_no, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        diagnostics.append(f"resource_report_malformed:line={line_no}")
                        continue
                    if not isinstance(entry, dict):
                        continue
                    entry_count += 1
                    msg_id = str(entry.get("message_id") or "").strip()
                    status = str(entry.get("status") or "").strip().lower()
                    status = status if status in _RESOURCE_STATES else "unknown"
                    status_counts[status] = status_counts.get(status, 0) + 1
                    reason = self._extract_report_reason(entry)
                    if reason:
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    if msg_id not in ("", "0") and msg_id not in ambiguous_ids:
                        fields = {"status": status, "reason": reason, "kind": entry.get("kind")}
                        previous = by_server_id.setdefault(msg_id, fields)
                        if previous != fields:
                            ambiguous_ids.add(msg_id)
                            del by_server_id[msg_id]
        except (OSError, UnicodeError):
            diagnostics.append("resource_report_read_error")

        diagnostics.append(f"resource_report:entries={entry_count}")
        for status in sorted(status_counts):
            diagnostics.append(f"resource_report_status:{status}={status_counts[status]}")
        for reason in sorted(reason_counts):
            diagnostics.append(f"resource_report_reason:{reason}={reason_counts[reason]}")
        return by_server_id, diagnostics

    @staticmethod
    def _extract_report_reason(entry: dict[str, Any] | None) -> str | None:
        if not isinstance(entry, dict):
            return None
        for key in ("image_diagnostics", "audio_diagnostics"):
            block = entry.get(key)
            if isinstance(block, dict):
                reason = str(block.get("reason") or "").strip()
                if reason in _REPORT_REASONS:
                    return reason
        return None

    def _pseudonymizer_for(self, group_id: str) -> Pseudonymizer:
        salt = (
            f"{self._explicit_scope_salt or ''}:{self.source_id}:{self.account_scope}:{group_id}"
        )
        return Pseudonymizer(salt)

    def _resolve_group_meta(self, metadata: dict[str, Any], records: list[dict[str, Any]]) -> tuple[str, str]:
        group_id = str(metadata.get("session") or "").strip()
        if not group_id:
            for record in records:
                session = str(record.get("session") or "").strip()
                if session:
                    group_id = session
                    break
        if not group_id:
            group_id = self.export_path.stem
        display_name = str(metadata.get("name") or group_id)
        return group_id, display_name

    def list_groups(self) -> list[GroupInfo]:
        metadata, records, _ = self._read_export()
        group_id, display_name = self._resolve_group_meta(metadata, records)
        return [
            GroupInfo(
                source_id=self.source_id,
                account_scope=self.account_scope,
                group_id=group_id,
                display_name=display_name,
                message_count_hint=len(records),
            )
        ]

    def _inspect_resource(
        self,
        res: dict[str, Any],
        *,
        media_id: str,
        msg_kind: str,
        report_reason: str | None,
    ) -> MediaRef:
        res_kind = str(res.get("kind") or msg_kind or "unknown").strip().lower()
        if msg_kind == "emoji" and res_kind == "image":
            ref_kind = "emoji"
        elif res_kind == "original" and msg_kind in MEDIA_KINDS:
            ref_kind = msg_kind
        else:
            ref_kind = res_kind

        raw_status = str(res.get("status") or "missing").strip().lower()
        role = _map_role(res.get("role"))
        if str(res.get("quality") or "").strip().lower() == "thumbnail":
            role = MediaRole.thumbnail
        quality = _map_quality(res.get("quality"), role, raw_status)
        raw_path = res.get("path")

        safe_path: Path | None = None
        normalized_rel: str | None = None
        if raw_path not in (None, ""):
            safe_path = resolve_safe_path(self.approved_root, raw_path, base=self.export_path.parent)
            if safe_path is None:
                return MediaRef(
                    media_id=media_id,
                    kind=ref_kind,
                    relative_path=None,
                    role=role,
                    quality=quality,
                    status=MediaStatus.failed,
                    reason="path_outside_root",
                )
            normalized_rel = safe_path.relative_to(self.approved_root).as_posix()

        if raw_status != "available":
            mapped_status, reason = _map_non_available_status(raw_status, report_reason)
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                role=role,
                quality=quality,
                status=mapped_status,
                reason=reason,
            )

        if safe_path is None:
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                role=role,
                quality=quality,
                status=MediaStatus.missing,
                reason="file_not_found",
            )
        try:
            if not safe_path.is_file():
                return MediaRef(
                    media_id=media_id,
                    kind=ref_kind,
                    relative_path=normalized_rel,
                    role=role,
                    quality=quality,
                    status=MediaStatus.missing,
                    reason="file_not_found",
                )
            file_size = safe_path.stat().st_size
        except OSError:
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                role=role,
                quality=quality,
                status=MediaStatus.failed,
                reason="read_error",
            )
        if file_size > MAX_MEDIA_FILE_BYTES:
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                role=role,
                quality=quality,
                status=MediaStatus.failed,
                reason="file_too_large",
            )

        try:
            content_hash = _sha256_file(safe_path)
        except OSError:
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                role=role,
                quality=quality,
                status=MediaStatus.failed,
                reason="read_error",
            )

        should_inspect_image = (
            ref_kind in ("image", "emoji")
            or safe_path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if should_inspect_image:
            if safe_path.suffix.lower() not in IMAGE_EXTENSIONS:
                # Refuse formats whose Pillow plugin may shell out to an external decoder.
                return MediaRef(
                    media_id=media_id,
                    kind=ref_kind,
                    relative_path=normalized_rel,
                    content_hash=content_hash,
                    role=role,
                    quality=quality,
                    status=MediaStatus.failed,
                    reason="unsupported_image_format",
                )
            try:
                with Image.open(safe_path, formats=tuple(sorted(SUPPORTED_IMAGE_FORMATS))) as img:
                    image_format = str(img.format or "").upper()
                    if image_format not in SUPPORTED_IMAGE_FORMATS:
                        return MediaRef(
                            media_id=media_id,
                            kind=ref_kind,
                            relative_path=normalized_rel,
                            content_hash=content_hash,
                            role=role,
                            quality=quality,
                            status=MediaStatus.failed,
                            reason="unsupported_image_format",
                        )
                    dimensions = img.size
                    frame_count = 0
                    pixels = 0
                    for frame in range(MAX_IMAGE_FRAMES + 1):
                        try:
                            img.seek(frame)
                        except EOFError:
                            if not frame_count:
                                raise
                            break
                        pixels += img.width * img.height
                        if frame == MAX_IMAGE_FRAMES or pixels > MAX_IMAGE_TOTAL_PIXELS:
                            return MediaRef(
                                media_id=media_id,
                                kind=ref_kind,
                                relative_path=normalized_rel,
                                role=role,
                                quality=quality,
                                status=MediaStatus.failed,
                                reason="too_many_frames" if frame == MAX_IMAGE_FRAMES else "too_many_pixels",
                            )
                        img.load()
                        frame_count += 1
                    is_animated = frame_count > 1
                    mime = img.get_format_mimetype()
            except Image.DecompressionBombError:
                return MediaRef(
                    media_id=media_id,
                    kind=ref_kind,
                    relative_path=normalized_rel,
                    content_hash=content_hash,
                    role=role,
                    quality=quality,
                    status=MediaStatus.failed,
                    reason="too_many_pixels",
                )
            except (OSError, SyntaxError, ValueError, EOFError, MemoryError):
                return MediaRef(
                    media_id=media_id,
                    kind=ref_kind,
                    relative_path=normalized_rel,
                    role=role,
                    quality=quality,
                    status=MediaStatus.failed,
                    reason="corrupt_image",
                )
            if not mime:
                mime = mimetypes.guess_type(safe_path.name)[0] or "application/octet-stream"
            return MediaRef(
                media_id=media_id,
                kind=ref_kind,
                relative_path=normalized_rel,
                mime=mime,
                content_hash=content_hash,
                dimensions=dimensions,
                frame_count=frame_count,
                is_animated=is_animated,
                role=role,
                quality=quality,
                status=MediaStatus.readable,
                reason=None,
            )

        mime = mimetypes.guess_type(safe_path.name)[0] or "application/octet-stream"
        return MediaRef(
            media_id=media_id,
            kind=ref_kind,
            relative_path=normalized_rel,
            mime=mime,
            content_hash=content_hash,
            role=role,
            quality=quality,
            status=MediaStatus.readable,
            reason=None,
        )

    def _build_media_refs(
        self,
        raw: dict[str, Any],
        *,
        owner_id: str,
        kind: str,
        report_entry: dict[str, Any] | None,
    ) -> tuple[MediaRef, ...]:
        report = report_entry if report_entry and report_entry["kind"] == kind else {}
        report_status = report.get("status")
        report_reason = report.get("reason")
        raw_resources = raw.get("resources")
        refs: list[MediaRef] = []

        if isinstance(raw_resources, list):
            for idx, res in enumerate(raw_resources):
                if not isinstance(res, dict):
                    continue
                refs.append(
                    self._inspect_resource(
                        res,
                        media_id=f"{owner_id}:m{idx}",
                        msg_kind=kind,
                        report_reason=report_reason if res.get("status") == report_status else None,
                    )
                )

        if kind in MEDIA_KINDS and not any(ref.role == MediaRole.main for ref in refs):
            raw_status = str(raw.get("resource_status") or "").strip().lower()
            if not raw_status and report_status in _RESOURCE_STATES:
                raw_status = report_status
            if raw_status in ("", "available"):
                status = MediaStatus.missing
                reason = "no_main_resource" if refs else "no_resource_attached"
            else:
                status, reason = _map_non_available_status(
                    raw_status, report_reason if raw_status == report_status else None,
                )
            refs.append(MediaRef(
                media_id=f"{owner_id}:main",
                kind=kind,
                role=MediaRole.main,
                status=status,
                reason=reason,
            ))

        return tuple(refs)

    def _parse_forward_tree(
        self,
        forward_obj: Any,
        *,
        parent_id: str,
        parent_sent_at: datetime,
        tz: tzinfo,
        pseudonymizer: Pseudonymizer,
        report_by_id: dict[str, dict[str, Any]],
        scope: tuple[str, str, str],
        depth: int,
        budget: list[int],
    ) -> tuple[tuple[ForwardChild, ...], bool]:
        if not isinstance(forward_obj, dict):
            return (), False
        raw_children = forward_obj.get("messages")
        truncated = bool(forward_obj.get("truncated", False) or forward_obj.get("notice"))
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

            child_svrid = str(item.get("server_id") or "").strip()
            child_id = f"{parent_id}/f{idx}"
            child_kind = str(item.get("kind") or "other").strip() or "other"
            child_sent_at = _parse_timestamp(item, tz, fallback=parent_sent_at)
            child_sender = pseudonymizer.pseudonymize(
                item.get("sender_wxid"),
                display_name=item.get("sender"),
                is_system=(child_kind == "system"),
            )
            child_extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
            child_report = report_by_id.get(child_svrid) if child_svrid else None
            child_media = self._build_media_refs(
                item,
                owner_id=child_id,
                kind=child_kind,
                report_entry=child_report,
            )
            child_link = link_meta(child_kind, child_extra)
            nested_children, nested_truncated = self._parse_forward_tree(
                item.get("forward"),
                parent_id=child_id,
                parent_sent_at=child_sent_at,
                tz=tz,
                pseudonymizer=pseudonymizer,
                report_by_id=report_by_id,
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
                    media_refs=child_media,
                    link_meta=child_link,
                    forward_children=nested_children,
                    truncated=nested_truncated,
                    reply_to=quote_ref(item, pseudonymizer, scope),
                )
            )
            truncated = truncated or nested_truncated
        return tuple(children), truncated

    def _normalize_record(
        self,
        raw: dict[str, Any],
        *,
        group_id: str,
        tz: tzinfo,
        sent_at: datetime,
        pseudonymizer: Pseudonymizer,
        report_by_id: dict[str, dict[str, Any]],
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
        report_entry = report_by_id.get(raw_server_id) if raw_server_id else None
        media_refs = self._build_media_refs(
            raw,
            owner_id=message_id,
            kind=kind,
            report_entry=report_entry,
        )
        link = link_meta(kind, extra)
        reply_to = quote_ref(raw, pseudonymizer, scope)
        forward_children, forward_truncated = self._parse_forward_tree(
            raw.get("forward"),
            parent_id=message_id,
            parent_sent_at=sent_at,
            tz=tz,
            pseudonymizer=pseudonymizer,
            report_by_id=report_by_id,
            scope=scope,
            depth=0,
            budget=[self.max_forward_children],
        )
        locator = LocalEvidenceLocator(
            source_type=self._source_type(),
            source_ref=self._source_ref(),
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

    def iter_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Iterator[Message]:
        metadata, records, _ = self._read_export()
        export_group_id, _ = self._resolve_group_meta(metadata, records)
        if group_id != export_group_id:
            raise KeyError(f"Group {group_id!r} not found in export (available: {export_group_id!r})")

        tz, _ = _resolve_timezone(metadata.get("time_zone"))
        pseudonymizer = self._pseudonymizer_for(export_group_id)
        report_by_id, _ = self._load_resource_report()

        yield from self._iter_records(records, group_id, time_range, tz, pseudonymizer, report_by_id)

    def _iter_records(self, records, group_id, time_range, tz, pseudonymizer, report_by_id):
        for raw in records:
            if raw.get("session") and raw["session"] != group_id:
                continue
            sent_at = _parse_timestamp(raw, tz)
            if time_range is not None and not time_range.contains(sent_at):
                continue
            yield self._normalize_record(
                raw,
                group_id=group_id,
                tz=tz,
                sent_at=sent_at,
                pseudonymizer=pseudonymizer,
                report_by_id=report_by_id,
            )

    def build_snapshot(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
    ) -> Snapshot:
        metadata, records, parse_diagnostics = self._read_export()
        export_group_id, _ = self._resolve_group_meta(metadata, records)
        if group_id != export_group_id:
            raise KeyError(f"Group {group_id!r} not found in export (available: {export_group_id!r})")

        tz, tz_name = _resolve_timezone(metadata.get("time_zone"))
        pseudonymizer = self._pseudonymizer_for(export_group_id)
        report_by_id, report_diagnostics = self._load_resource_report()

        builder = SnapshotBuilder(
            source_id=self.source_id,
            account_scope=self.account_scope,
            group_id=export_group_id,
            time_range=time_range,
            timezone_name=tz_name,
        )
        for diag in (*parse_diagnostics, *report_diagnostics):
            builder.add_diagnostic(diag)

        builder.add_messages(self._iter_records(records, group_id, time_range, tz, pseudonymizer, report_by_id))

        return builder.build()

    def page_messages(
        self,
        group_id: str,
        time_range: TimeRange | None = None,
        *,
        limit: int = 100,
        after: MessageCursor | None = None,
    ) -> MessagePage:
        return self.build_snapshot(group_id, time_range).page_messages(limit=limit, after=after)
