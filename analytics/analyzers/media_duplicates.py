"""Deterministic image, GIF and sticker duplicate detection.

The first pass groups readable main media by SHA-256. Perceptual candidates are
kept separate from exact duplicates, and missing/unreadable/thumbnail assets are
never treated as unique or duplicate evidence.

Configuration (``AnalysisContext.config``):
- ``media_root``: optional absolute approved root for near-image reads. When
  supplied, available files are re-hashed for exact grouping; without it valid
  snapshot hashes are used and perceptual candidates are skipped.
- ``phash_threshold``: 0..63 Hamming threshold, default 10.
- ``max_candidate_pairs`` / ``max_rejected_pairs`` / ``max_near_groups``:
  bounded near comparison and rejected-edge retention.
- ``max_frames``: sampled GIF/WebP frames, 2..10 (default 5).
- ``max_clusters`` / ``max_member_ids`` / ``max_evidence_ids``: bounded output.

Output keeps ``clusters`` (exact SHA-256 groups with one caption, or
``same_meme_template`` groups with changed captions) separate from ``candidates``
(near, same-template or uncertain pairs) and bounded ``rejected`` edges.
Caption comparison uses normalized message text only and emits a count, not
caption hashes; OCR is intentionally not part of CL-03.
"""
from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from analytics.analyzer import (
    AnalysisContext,
    AnalysisResult,
    Analyzer,
    Cancellation,
    ProgressReporter,
)
from analytics.media_limits import (
    MAX_IMAGE_FRAMES,
    MAX_IMAGE_TOTAL_PIXELS,
    MAX_MEDIA_FILE_BYTES,
    SUPPORTED_IMAGE_FORMATS,
)
from analytics.models import (
    ForwardChild,
    MediaQuality,
    MediaRef,
    MediaRole,
    MediaStatus,
    Snapshot,
)
from analytics.privacy import resolve_safe_path

_IMAGE_KINDS = frozenset({"image", "emoji", "sticker"})
_PLACEHOLDER_CAPTIONS = frozenset({
    "[图片]", "[表情]", "[动画表情]", "[GIF]", "[gif]", "[视频]", "[语音]", "[文件]",
})
_MAX_ASPECT_DELTA = 0.25
_UNCERTAIN_ASPECT_DELTA = 0.05
_MAX_DURATION_DELTA = 0.25
_UNCERTAIN_DURATION_DELTA = 0.05
_DCT_SIZE = 32
_HASH_SIZE = 8
_COS = tuple(
    tuple(math.cos((2 * position + 1) * frequency * math.pi / (2 * _DCT_SIZE)) for position in range(_DCT_SIZE))
    for frequency in range(_HASH_SIZE)
)


@dataclass(frozen=True)
class _Config:
    media_root: Path | None = None
    phash_threshold: int = 10
    max_candidate_pairs: int = 1000
    max_rejected_pairs: int = 1000
    max_near_groups: int = 5000
    max_frames: int = 5
    max_clusters: int = 1000
    max_member_ids: int = 1000
    max_evidence_ids: int = 5000

    @classmethod
    def from_context(cls, context: AnalysisContext) -> "_Config":
        raw = context.config
        media_root = raw.get("media_root")
        if media_root is not None:
            if not isinstance(media_root, (str, Path)):
                raise ValueError("media_root must be a path string or Path")
            text = str(media_root).strip()
            if not text:
                raise ValueError("media_root must not be empty")
            path = Path(text).expanduser()
            if not path.is_absolute():
                raise ValueError("media_root must be an absolute path")
            media_root = path.resolve()
        return cls(
            media_root=media_root,
            phash_threshold=_bounded_int(raw, "phash_threshold", 10, 0, 63),
            max_candidate_pairs=_bounded_int(raw, "max_candidate_pairs", 1000, 1, 100_000),
            max_rejected_pairs=_bounded_int(raw, "max_rejected_pairs", 1000, 1, 100_000),
            max_near_groups=_bounded_int(raw, "max_near_groups", 5000, 1, 100_000),
            max_frames=_bounded_int(raw, "max_frames", 5, 2, 10),
            max_clusters=_bounded_int(raw, "max_clusters", 1000, 1, 100_000),
            max_member_ids=_bounded_int(raw, "max_member_ids", 1000, 1, 100_000),
            max_evidence_ids=_bounded_int(raw, "max_evidence_ids", 5000, 1, 100_000),
        )


def _bounded_int(raw: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


def _normalize_sha256(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        return None
    return normalized


@dataclass(frozen=True)
class _Occurrence:
    evidence_id: str
    kind: str
    caption: str
    sender: str
    sent_at: datetime
    content_hash: str
    file_path: Path | None
    dimensions: tuple[int, int] | None
    frame_count: int | None
    is_animated: bool | None


@dataclass(frozen=True)
class _Owner:
    evidence_id: str
    text: str
    sender: str
    sent_at: datetime
    kind: str
    refs: tuple[MediaRef, ...]
    children: tuple[ForwardChild, ...]


@dataclass(frozen=True)
class _Signature:
    dimensions: tuple[int, int]
    frame_count: int
    frame_hashes: tuple[int, ...]
    duration_ms: int | None


_KIND_STAT_KEYS = (
    "media_refs",
    "eligible",
    "analyzed",
    "thumbnail",
    "unsupported_kind",
    "missing",
    "unsupported",
    "failed",
    "not_requested",
    "no_path",
    "outside_root",
    "read_error",
    "invalid_hash",
    "hash_mismatch",
    "exact_occurrences",
    "template_occurrences",
)


def _new_kind_stats() -> dict[str, int]:
    return {key: 0 for key in _KIND_STAT_KEYS}


def _normalized_caption(text: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", str(text or "")).split())
    if normalized in _PLACEHOLDER_CAPTIONS:
        return ""
    return normalized


def _is_image_ref(ref: MediaRef, owner_kind: str) -> bool:
    if ref.kind in _IMAGE_KINDS:
        return True
    return (
        owner_kind in _IMAGE_KINDS
        and ref.role is MediaRole.main
        and (ref.mime or "").lower().startswith("image/")
    )


def _phash(image: Image.Image) -> int:
    """Return a 63-bit DCT perceptual hash for a decoded image."""
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    gray = image.convert("L").resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    rows = [
        pixels[offset:offset + _DCT_SIZE]
        for offset in range(0, _DCT_SIZE * _DCT_SIZE, _DCT_SIZE)
    ]
    row_coefficients = [
        [sum(row[x] * cos_u[x] for x in range(_DCT_SIZE)) for cos_u in _COS]
        for row in rows
    ]
    coefficients: list[float] = []
    for u in range(_HASH_SIZE):
        for v in range(_HASH_SIZE):
            if u == 0 and v == 0:
                continue
            cos_v = _COS[v]
            coefficients.append(sum(
                row_coefficients[y][u] * cos_v[y]
                for y in range(_DCT_SIZE)
            ))
    threshold = sorted(coefficients)[len(coefficients) // 2]
    bits = 0
    for value in coefficients:
        bits = (bits << 1) | (1 if value > threshold else 0)
    return bits


def _sample_indices(frame_count: int, limit: int) -> tuple[int, ...]:
    if frame_count <= limit:
        return tuple(range(frame_count))
    if limit == 1:
        return (0,)
    return tuple(sorted({round(index * (frame_count - 1) / (limit - 1)) for index in range(limit)}))


def _load_signature(path: Path, *, max_frames: int) -> _Signature | None:
    try:
        if path.stat().st_size > MAX_MEDIA_FILE_BYTES:
            return None
        with Image.open(path, formats=tuple(sorted(SUPPORTED_IMAGE_FORMATS))) as opened:
            if (opened.format or "").upper() not in SUPPORTED_IMAGE_FORMATS:
                return None
            width, height = opened.size
            if width <= 0 or height <= 0:
                return None
            frame_count = int(getattr(opened, "n_frames", 1))
            if not 1 <= frame_count <= MAX_IMAGE_FRAMES:
                return None
            if width * height * frame_count > MAX_IMAGE_TOTAL_PIXELS:
                return None
            if frame_count == 1:
                frame = ImageOps.exif_transpose(opened)
                if frame is None:
                    return None
                return _Signature(frame.size, 1, (_phash(frame),), None)
            samples = set(_sample_indices(frame_count, max_frames))
            hashes: list[int] = []
            duration_ms = 0
            for index in range(frame_count):
                opened.seek(index)
                # Pillow WebP exposes duration only after the frame is loaded.
                opened.load()
                duration_ms += int(opened.info.get("duration", 0) or 0)
                if index in samples:
                    hashes.append(_phash(opened.copy()))
            return _Signature((width, height), frame_count, tuple(hashes), duration_ms)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        TypeError,
        OverflowError,
        SyntaxError,
        EOFError,
        MemoryError,
    ):
        return None


def _aspect_delta(left: tuple[int, int], right: tuple[int, int]) -> float:
    if left[0] <= 0 or left[1] <= 0 or right[0] <= 0 or right[1] <= 0:
        return float("inf")
    left_ratio = left[0] / left[1]
    right_ratio = right[0] / right[1]
    return abs(left_ratio - right_ratio) / max(left_ratio, right_ratio)


def _duration_delta(left: int | None, right: int | None) -> float:
    if left is None or right is None:
        return 0.0
    return abs(left - right) / max(1, max(left, right))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _group_hint(group: list[_Occurrence]) -> _Occurrence:
    return next((item for item in group if item.dimensions is not None), group[0])


def _hint_reason(left: _Occurrence, right: _Occurrence) -> str | None:
    left_animated = left.frame_count > 1 if left.frame_count is not None else left.is_animated
    right_animated = right.frame_count > 1 if right.frame_count is not None else right.is_animated
    if left_animated is not None and right_animated is not None and left_animated != right_animated:
        return "animation"
    if (
        left.frame_count is not None
        and right.frame_count is not None
        and left.frame_count != right.frame_count
    ):
        return "frame_count"
    if left.dimensions is not None and right.dimensions is not None:
        if _aspect_delta(left.dimensions, right.dimensions) > _MAX_ASPECT_DELTA:
            return "aspect"
    return None


def _iter_media(snapshot: Snapshot) -> tuple[list[_Owner], int]:
    """Return media owners in document order and the total ref count."""
    owners: list[_Owner] = []
    total_refs = 0
    stack = [
        _Owner(
            evidence_id=message.message_id,
            text=message.text,
            sender=message.sender_pseudonym,
            sent_at=message.sent_at,
            kind=message.kind,
            refs=message.media_refs,
            children=message.forward_children,
        )
        for message in reversed(snapshot.messages)
    ]
    while stack:
        owner = stack.pop()
        owners.append(owner)
        total_refs += len(owner.refs)
        for child in reversed(owner.children):
            stack.append(_Owner(
                evidence_id=child.child_id,
                text=child.text,
                sender=child.sender_pseudonym,
                sent_at=child.sent_at or owner.sent_at,
                kind=child.kind,
                refs=child.media_refs,
                children=child.forward_children,
            ))
    return owners, total_refs


class MediaDuplicateAnalyzer(Analyzer):
    """Exact and near duplicate detection for local image, GIF and sticker bytes."""

    analyzer_id = "media.duplicates"
    version = "1"
    supported_message_types = frozenset()
    estimated_workload = 4

    def run(
        self,
        snapshot: Snapshot,
        context: AnalysisContext,
        cancellation: Cancellation,
        progress: ProgressReporter,
    ) -> AnalysisResult:
        config = _Config.from_context(context)
        owners, total_refs = _iter_media(snapshot)
        excluded = {
            "thumbnail": 0,
            "unsupported_kind": 0,
            "missing": 0,
            "unsupported": 0,
            "failed": 0,
            "not_requested": 0,
            "no_path": 0,
            "outside_root": 0,
            "read_error": 0,
            "invalid_hash": 0,
        }
        by_kind: dict[str, dict[str, int]] = {}
        occurrences: list[_Occurrence] = []
        analyzed_evidence: set[str] = set()
        eligible = 0
        near_ready = 0
        hash_mismatches = 0

        for index, owner in enumerate(owners):
            cancellation.check()
            if index % 64 == 0 and total_refs:
                progress(min(0.5, 0.5 * index / max(1, len(owners))), "scanning media")
            for ref in owner.refs:
                kind_stats = by_kind.setdefault(str(ref.kind), _new_kind_stats())
                kind_stats["media_refs"] += 1
                if not _is_image_ref(ref, owner.kind):
                    excluded["unsupported_kind"] += 1
                    kind_stats["unsupported_kind"] += 1
                    continue
                if ref.role is MediaRole.thumbnail or ref.quality is MediaQuality.thumbnail:
                    excluded["thumbnail"] += 1
                    kind_stats["thumbnail"] += 1
                    continue
                eligible += 1
                kind_stats["eligible"] += 1
                if ref.status is not MediaStatus.readable:
                    key = ref.status.value if ref.status.value in excluded else "failed"
                    excluded[key] += 1
                    kind_stats[key] += 1
                    continue

                raw_content_hash = ref.content_hash
                content_hash = _normalize_sha256(raw_content_hash)
                invalid_hash = raw_content_hash is not None and content_hash is None
                relative_path = ref.relative_path
                readable_path: Path | None = None
                if relative_path is not None:
                    if config.media_root is None:
                        # Exact grouping can still use a valid snapshot hash without reading bytes.
                        if content_hash is None:
                            key = "invalid_hash" if invalid_hash else "no_path"
                            excluded[key] += 1
                            kind_stats[key] += 1
                            continue
                    else:
                        readable_path = resolve_safe_path(config.media_root, relative_path)
                        if readable_path is None:
                            excluded["outside_root"] += 1
                            kind_stats["outside_root"] += 1
                            continue
                        try:
                            if not readable_path.is_file():
                                excluded["missing"] += 1
                                kind_stats["missing"] += 1
                                continue
                            if readable_path.stat().st_size > MAX_MEDIA_FILE_BYTES:
                                excluded["read_error"] += 1
                                kind_stats["read_error"] += 1
                                continue
                            actual_hash = _sha256_file(readable_path)
                            if content_hash is not None and content_hash != actual_hash:
                                hash_mismatches += 1
                                kind_stats["hash_mismatch"] += 1
                            content_hash = actual_hash
                        except OSError:
                            excluded["read_error"] += 1
                            kind_stats["read_error"] += 1
                            continue
                if content_hash is None:
                    key = "invalid_hash" if invalid_hash else "no_path"
                    excluded[key] += 1
                    kind_stats[key] += 1
                    continue

                analyzed_evidence.add(owner.evidence_id)
                kind_stats["analyzed"] += 1
                occurrences.append(_Occurrence(
                    evidence_id=owner.evidence_id,
                    kind=ref.kind,
                    caption=_normalized_caption(owner.text),
                    sender=owner.sender,
                    sent_at=owner.sent_at,
                    content_hash=str(content_hash).lower(),
                    file_path=readable_path,
                    dimensions=ref.dimensions,
                    frame_count=ref.frame_count,
                    is_animated=ref.is_animated,
                ))
                if readable_path is not None:
                    near_ready += 1

        progress(0.6, "grouping exact media")
        groups: dict[str, list[_Occurrence]] = {}
        for occurrence in occurrences:
            groups.setdefault(occurrence.content_hash, []).append(occurrence)

        clusters: list[dict[str, Any]] = []
        clusters_truncated = 0
        exact_cluster_count = 0
        template_cluster_count = 0
        exact_occurrences = 0
        template_occurrences = 0
        confirmed_duplicates = 0
        for content_hash, group in sorted(groups.items()):
            if len(group) < 2:
                continue
            group = sorted(group, key=lambda item: (item.sent_at, item.evidence_id))
            caption_variant_count = len({item.caption for item in group})
            relation = (
                "same_meme_template"
                if caption_variant_count > 1
                else "exact_duplicate"
            )
            prefix = "template" if relation == "same_meme_template" else "exact"
            if relation == "same_meme_template":
                template_cluster_count += 1
                template_occurrences += len(group)
            else:
                exact_cluster_count += 1
                exact_occurrences += len(group)
                confirmed_duplicates += len(group) - 1
            for item in group:
                kind_stats = by_kind.setdefault(item.kind, _new_kind_stats())
                key = "template_occurrences" if relation == "same_meme_template" else "exact_occurrences"
                kind_stats[key] += 1
            if len(clusters) >= config.max_clusters:
                clusters_truncated += 1
                continue
            evidence_ids = sorted({item.evidence_id for item in group})
            member_ids_truncated = len(evidence_ids) > config.max_member_ids
            clusters.append({
                "cluster_id": f"{prefix}:{content_hash}",
                "relation": relation,
                "representative_evidence_id": group[0].evidence_id,
                "member_evidence_ids": evidence_ids[:config.max_member_ids],
                "member_ids_truncated": member_ids_truncated,
                "occurrence_count": len(group),
                "unique_sender_count": len({item.sender for item in group}),
                "first_seen": _iso(group[0].sent_at),
                "last_seen": _iso(group[-1].sent_at),
                "media_kinds": sorted({item.kind for item in group}),
                "caption_variant_count": caption_variant_count,
                "caption_changed": caption_variant_count > 1,
                "content_hash": content_hash,
            })

        diagnostics: list[str] = []
        if hash_mismatches:
            diagnostics.append(f"content_hash_mismatch:{hash_mismatches}")
        candidates: list[dict[str, Any]] = []
        signatures: dict[str, _Signature] = {}
        rejected: list[dict[str, Any]] = []
        rejected_truncated = False
        near_excluded = {
            "no_signature": 0,
            "animation": 0,
            "frame_count": 0,
            "aspect": 0,
            "duration": 0,
            "visual_distance": 0,
        }
        pairs_compared = 0
        pair_limit_reached = False
        near_detection = "enabled"

        def add_rejected(
            reason: str,
            left_hash: str,
            right_hash: str,
            **details: Any,
        ) -> None:
            nonlocal rejected_truncated
            if len(rejected) >= config.max_rejected_pairs:
                rejected_truncated = True
                return
            left_group = groups[left_hash]
            right_group = groups[right_hash]
            left_rep = min(left_group, key=lambda item: (item.sent_at, item.evidence_id))
            right_rep = min(right_group, key=lambda item: (item.sent_at, item.evidence_id))
            rejected.append({
                "left_group_id": f"media:{left_hash}",
                "right_group_id": f"media:{right_hash}",
                "left_evidence_id": left_rep.evidence_id,
                "right_evidence_id": right_rep.evidence_id,
                "reason": reason,
                **details,
            })

        if config.media_root is None:
            near_detection = "skipped:media_root_missing"
            diagnostics.append("near_detection_skipped:media_root_missing")
        elif not config.media_root.is_dir():
            near_detection = "skipped:media_root_not_directory"
            diagnostics.append("near_detection_skipped:media_root_not_directory")
        elif len(groups) > config.max_near_groups:
            near_detection = "skipped:too_many_media_groups"
            diagnostics.append("near_detection_skipped:too_many_media_groups")
        elif not occurrences:
            near_detection = "empty"
        else:
            progress(0.7, "prefiltering media")
            ordered_hashes = sorted(groups)
            candidate_pairs: list[tuple[str, str]] = []
            stop = False
            for left_index, left_hash in enumerate(ordered_hashes):
                if stop:
                    break
                for right_index in range(left_index + 1, len(ordered_hashes)):
                    if pairs_compared >= config.max_candidate_pairs:
                        pair_limit_reached = True
                        stop = True
                        break
                    pairs_compared += 1
                    cancellation.check()
                    right_hash = ordered_hashes[right_index]
                    reason = _hint_reason(
                        _group_hint(groups[left_hash]),
                        _group_hint(groups[right_hash]),
                    )
                    if reason is not None:
                        near_excluded[reason] += 1
                        add_rejected(reason, left_hash, right_hash)
                        continue
                    candidate_pairs.append((left_hash, right_hash))

            progress(0.8, "hashing media")
            needed_hashes = sorted({
                content_hash
                for pair in candidate_pairs
                for content_hash in pair
            })
            for content_hash in needed_hashes:
                cancellation.check()
                ordered = sorted(groups[content_hash], key=lambda item: (item.sent_at, item.evidence_id))
                occurrence = next((item for item in ordered if item.file_path), None)
                signature = (
                    _load_signature(occurrence.file_path, max_frames=config.max_frames)
                    if occurrence is not None and occurrence.file_path is not None
                    else None
                )
                if signature is None:
                    near_excluded["no_signature"] += 1
                    for pair in candidate_pairs:
                        if content_hash in pair:
                            add_rejected("signature_unavailable", *pair)
                else:
                    signatures[content_hash] = signature

            progress(0.85, "comparing media")
            for left_hash, right_hash in candidate_pairs:
                left = signatures.get(left_hash)
                right = signatures.get(right_hash)
                if left is None or right is None:
                    continue
                cancellation.check()
                if (left.frame_count > 1) != (right.frame_count > 1):
                    near_excluded["animation"] += 1
                    add_rejected("animation", left_hash, right_hash)
                    continue
                if left.frame_count != right.frame_count:
                    near_excluded["frame_count"] += 1
                    add_rejected("frame_count", left_hash, right_hash)
                    continue
                aspect_delta = _aspect_delta(left.dimensions, right.dimensions)
                if aspect_delta > _MAX_ASPECT_DELTA:
                    near_excluded["aspect"] += 1
                    add_rejected("aspect", left_hash, right_hash)
                    continue
                duration_delta = _duration_delta(left.duration_ms, right.duration_ms)
                if duration_delta > _MAX_DURATION_DELTA:
                    near_excluded["duration"] += 1
                    add_rejected("duration", left_hash, right_hash)
                    continue
                distance = max(
                    (left_frame ^ right_frame).bit_count()
                    for left_frame, right_frame in zip(left.frame_hashes, right.frame_hashes)
                )
                if distance > config.phash_threshold:
                    near_excluded["visual_distance"] += 1
                    add_rejected(
                        "visual_distance",
                        left_hash,
                        right_hash,
                        hamming_distance=distance,
                        aspect_delta=round(aspect_delta, 4),
                        duration_delta=round(duration_delta, 4),
                        frame_count=[left.frame_count, right.frame_count],
                    )
                    continue
                left_group = groups[left_hash]
                right_group = groups[right_hash]
                captions_changed = {item.caption for item in left_group} != {
                    item.caption for item in right_group
                }
                if captions_changed:
                    relation = "same_meme_template"
                    reason = "caption_changed"
                elif aspect_delta > _UNCERTAIN_ASPECT_DELTA:
                    relation = "uncertain"
                    reason = "aspect_changed"
                elif duration_delta > _UNCERTAIN_DURATION_DELTA:
                    relation = "uncertain"
                    reason = "duration_changed"
                else:
                    relation = "near_duplicate"
                    reason = None
                left_rep = min(left_group, key=lambda item: (item.sent_at, item.evidence_id))
                right_rep = min(right_group, key=lambda item: (item.sent_at, item.evidence_id))
                candidates.append({
                    "candidate_id": f"near:{left_hash}:{right_hash}",
                    "left_group_id": f"media:{left_hash}",
                    "right_group_id": f"media:{right_hash}",
                    "left_evidence_id": left_rep.evidence_id,
                    "right_evidence_id": right_rep.evidence_id,
                    "left_occurrences": len(left_group),
                    "right_occurrences": len(right_group),
                    "relation": relation,
                    "reason": reason,
                    "hamming_distance": distance,
                    "score": round(1.0 - distance / 63.0, 4),
                    "aspect_delta": round(aspect_delta, 4),
                    "duration_delta": round(duration_delta, 4),
                    "frame_count": [left.frame_count, right.frame_count],
                    "caption_changed": captions_changed,
                })
            if pair_limit_reached:
                diagnostics.append(f"near_pair_limit_reached:{config.max_candidate_pairs}")
                near_detection = "partial"
            if near_excluded["no_signature"]:
                diagnostics.append(f"near_signature_unavailable:{near_excluded['no_signature']}")
                near_detection = "partial"
            if rejected_truncated:
                diagnostics.append(f"near_rejected_truncated:{config.max_rejected_pairs}")
                near_detection = "partial"
            if near_ready < len(occurrences) and near_detection == "enabled":
                near_detection = "partial"

        if clusters_truncated:
            diagnostics.append(f"clusters_truncated:{clusters_truncated}")
        sorted_evidence = sorted(analyzed_evidence)
        evidence_truncated = len(sorted_evidence) > config.max_evidence_ids
        if evidence_truncated:
            diagnostics.append(
                f"evidence_ids_truncated:{len(sorted_evidence) - config.max_evidence_ids}"
            )
        coverage = {
            "top_level_messages": len(snapshot.messages),
            "media_refs_seen": total_refs,
            "eligible": eligible,
            "analyzed": len(occurrences),
            "unique_media_groups": len(groups),
            "exact_clusters": exact_cluster_count,
            "template_clusters": template_cluster_count,
            "clusters_returned": len(clusters),
            "clusters_truncated": clusters_truncated,
            "exact_occurrences": exact_occurrences,
            "template_occurrences": template_occurrences,
            "confirmed_duplicate_occurrences": confirmed_duplicates,
            "hash_mismatch": hash_mismatches,
            "by_kind": by_kind,
            "excluded": excluded,
            "near_detection": near_detection,
            "near_eligible": near_ready,
            "near_signatures": len(signatures),
            "near_pairs_compared": pairs_compared,
            "near_candidates": len(candidates),
            "near_template_candidates": sum(
                1 for candidate in candidates if candidate["relation"] == "same_meme_template"
            ),
            "near_uncertain_candidates": sum(
                1 for candidate in candidates if candidate["relation"] == "uncertain"
            ),
            "near_rejected_returned": len(rejected),
            "near_rejected_truncated": rejected_truncated,
            "near_excluded": near_excluded,
            "evidence_ids_returned": min(len(sorted_evidence), config.max_evidence_ids),
            "evidence_ids_truncated": evidence_truncated,
        }
        output = {
            "analyzer": self.analyzer_id,
            "version": self.version,
            "config": {
                "phash_threshold": config.phash_threshold,
                "phash_algorithm": "phash63-dct-8x8-ac",
                "caption_source": "message_text",
                "near_detection": near_detection,
                "media_root_supplied": config.media_root is not None,
                "max_frames": config.max_frames,
                "max_candidate_pairs": config.max_candidate_pairs,
                "max_rejected_pairs": config.max_rejected_pairs,
                "max_near_groups": config.max_near_groups,
                "max_clusters": config.max_clusters,
                "max_member_ids": config.max_member_ids,
                "max_evidence_ids": config.max_evidence_ids,
            },
            "coverage": coverage,
            "clusters": clusters,
            "candidates": candidates,
            "rejected": rejected,
        }
        progress(0.95, "finalizing media duplicates")
        return AnalysisResult(
            output=output,
            evidence_ids=tuple(sorted_evidence[:config.max_evidence_ids]),
            diagnostics=tuple(diagnostics),
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
