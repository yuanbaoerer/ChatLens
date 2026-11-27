"""Deterministic image, GIF and sticker duplicate detection.

The first pass groups readable main media by SHA-256. Perceptual candidates are
kept separate from exact duplicates, and missing/unreadable/thumbnail assets are
never treated as unique or duplicate evidence.

Configuration (``AnalysisContext.config``):
- ``media_root``: optional approved root for near-image reads. Without it exact
  hash grouping still works, but perceptual candidates are skipped.
- ``phash_threshold``: 0..64 Hamming threshold, default 10.
- ``max_candidate_pairs`` / ``max_near_groups``: bounded near comparison.
- ``max_frames``: sampled GIF frames, 2..10 (default 5).

Output keeps ``clusters`` (exact SHA-256 groups) separate from ``candidates``
(near, same-template or uncertain pairs). Caption comparison uses normalized
message text only; OCR is intentionally not part of CL-03.
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
_SUPPORTED_IMAGE_FORMATS = frozenset({"BMP", "GIF", "JPEG", "PNG", "TIFF", "WEBP"})
_MAX_IMAGE_FRAMES = 1000
_MAX_IMAGE_TOTAL_PIXELS = 64_000_000
_MAX_MEDIA_FILE_BYTES = 256 * 1024 * 1024
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
    max_near_groups: int = 5000
    max_frames: int = 5

    @classmethod
    def from_context(cls, context: AnalysisContext) -> "_Config":
        raw = context.config
        media_root = raw.get("media_root")
        if media_root is not None:
            if not isinstance(media_root, (str, Path)):
                raise ValueError("media_root must be a path string or Path")
            media_root = Path(media_root).expanduser().resolve()
        return cls(
            media_root=media_root,
            phash_threshold=_bounded_int(raw, "phash_threshold", 10, 0, 64),
            max_candidate_pairs=_bounded_int(raw, "max_candidate_pairs", 1000, 1, 100_000),
            max_near_groups=_bounded_int(raw, "max_near_groups", 5000, 1, 100_000),
            max_frames=_bounded_int(raw, "max_frames", 5, 2, 10),
        )


def _bounded_int(raw: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class _Occurrence:
    evidence_id: str
    kind: str
    caption: str
    sender: str
    sent_at: datetime
    content_hash: str
    file_path: Path | None


@dataclass(frozen=True)
class _Signature:
    dimensions: tuple[int, int]
    frame_count: int
    frame_hashes: tuple[int, ...]
    duration_ms: int | None


def _normalized_caption(text: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", str(text or "")).split())
    if normalized in _PLACEHOLDER_CAPTIONS:
        return ""
    return normalized


def _caption_key(caption: str) -> str:
    if not caption:
        return "none"
    return hashlib.sha256(caption.encode("utf-8")).hexdigest()[:16]


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
        if path.stat().st_size > _MAX_MEDIA_FILE_BYTES:
            return None
        with Image.open(path, formats=tuple(sorted(_SUPPORTED_IMAGE_FORMATS))) as opened:
            if (opened.format or "").upper() not in _SUPPORTED_IMAGE_FORMATS:
                return None
            width, height = opened.size
            if width <= 0 or height <= 0:
                return None
            frame_count = int(getattr(opened, "n_frames", 1))
            if not 1 <= frame_count <= _MAX_IMAGE_FRAMES:
                return None
            if width * height * frame_count > _MAX_IMAGE_TOTAL_PIXELS:
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
                duration_ms += int(opened.info.get("duration", 0) or 0)
                if index in samples:
                    hashes.append(_phash(opened.copy()))
            return _Signature((width, height), frame_count, tuple(hashes), duration_ms)
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        MemoryError,
    ):
        return None


def _aspect_delta(left: tuple[int, int], right: tuple[int, int]) -> float:
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


def _iter_media(
    snapshot: Snapshot,
) -> tuple[list[tuple[str, str, str, datetime, str, tuple[MediaRef, ...]]], int]:
    """Return media owners in document order and the total ref count."""
    owners: list[tuple[str, str, str, datetime, str, tuple[MediaRef, ...]]] = []
    total_refs = 0
    stack: list[tuple[
        str,
        str,
        str,
        datetime,
        str,
        tuple[MediaRef, ...],
        tuple[ForwardChild, ...],
    ]] = [
        (
            message.message_id,
            message.text,
            message.sender_pseudonym,
            message.sent_at,
            message.kind,
            message.media_refs,
            message.forward_children,
        )
        for message in reversed(snapshot.messages)
    ]
    while stack:
        evidence_id, text, sender, sent_at, kind, refs, children = stack.pop()
        owners.append((evidence_id, text, sender, sent_at, kind, refs))
        total_refs += len(refs)
        for child in reversed(tuple(children)):
            stack.append((
                child.child_id,
                child.text,
                child.sender_pseudonym,
                child.sent_at or sent_at,
                child.kind,
                child.media_refs,
                child.forward_children,
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
            "missing": 0,
            "unsupported": 0,
            "failed": 0,
            "not_requested": 0,
            "no_path": 0,
            "outside_root": 0,
            "read_error": 0,
        }
        occurrences: list[_Occurrence] = []
        analyzed_evidence: set[str] = set()
        eligible = 0
        near_ready = 0

        for index, (evidence_id, text, sender, sent_at, owner_kind, refs) in enumerate(owners):
            cancellation.check()
            if index % 64 == 0 and total_refs:
                progress(min(0.5, 0.5 * index / max(1, len(owners))), "scanning media")
            for ref in refs:
                if not _is_image_ref(ref, owner_kind):
                    continue
                if ref.role is MediaRole.thumbnail or ref.quality is MediaQuality.thumbnail:
                    excluded["thumbnail"] += 1
                    continue
                eligible += 1
                if ref.status is not MediaStatus.readable:
                    excluded[ref.status.value if ref.status.value in excluded else "failed"] += 1
                    continue

                content_hash = ref.content_hash
                relative_path = ref.relative_path
                readable_path: Path | None = None
                if relative_path is not None:
                    if config.media_root is None:
                        # Exact grouping can still use the snapshot hash without reading bytes.
                        if content_hash is None:
                            excluded["no_path"] += 1
                            continue
                    else:
                        readable_path = resolve_safe_path(config.media_root, relative_path)
                        if readable_path is None:
                            excluded["outside_root"] += 1
                            continue
                        try:
                            if not readable_path.is_file():
                                excluded["missing"] += 1
                                continue
                            if readable_path.stat().st_size > _MAX_MEDIA_FILE_BYTES:
                                excluded["read_error"] += 1
                                continue
                            if content_hash is None:
                                content_hash = _sha256_file(readable_path)
                        except OSError:
                            excluded["read_error"] += 1
                            continue
                if content_hash is None:
                    excluded["no_path"] += 1
                    continue

                analyzed_evidence.add(evidence_id)
                occurrences.append(_Occurrence(
                    evidence_id=evidence_id,
                    kind=ref.kind,
                    caption=_normalized_caption(text),
                    sender=sender,
                    sent_at=sent_at,
                    content_hash=str(content_hash).lower(),
                    file_path=readable_path,
                ))
                if readable_path is not None:
                    near_ready += 1

        progress(0.6, "grouping exact media")
        groups: dict[str, list[_Occurrence]] = {}
        for occurrence in occurrences:
            groups.setdefault(occurrence.content_hash, []).append(occurrence)

        clusters: list[dict[str, Any]] = []
        for content_hash, group in sorted(groups.items()):
            if len(group) < 2:
                continue
            group = sorted(group, key=lambda item: (item.sent_at, item.evidence_id))
            captions = sorted({_caption_key(item.caption) for item in group})
            evidence_ids = sorted({item.evidence_id for item in group})
            clusters.append({
                "cluster_id": f"exact:{content_hash}",
                "relation": "exact_duplicate",
                "representative_evidence_id": group[0].evidence_id,
                "member_evidence_ids": evidence_ids,
                "occurrence_count": len(group),
                "unique_sender_count": len({item.sender for item in group}),
                "first_seen": _iso(group[0].sent_at),
                "last_seen": _iso(group[-1].sent_at),
                "media_kinds": sorted({item.kind for item in group}),
                "caption_variants": captions,
                "caption_changed": len(captions) > 1,
                "content_hash": content_hash,
            })

        diagnostics: list[str] = []
        candidates: list[dict[str, Any]] = []
        signatures: dict[str, _Signature] = {}
        near_excluded = {
            "no_signature": 0,
            "animation": 0,
            "frame_count": 0,
            "aspect": 0,
            "duration": 0,
        }
        pairs_compared = 0
        pair_limit_reached = False
        near_detection = "enabled"

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
            progress(0.7, "hashing media")
            for content_hash, group in sorted(groups.items()):
                cancellation.check()
                ordered = sorted(group, key=lambda item: (item.sent_at, item.evidence_id))
                occurrence = next((item for item in ordered if item.file_path), None)
                if occurrence is None or occurrence.file_path is None:
                    near_excluded["no_signature"] += 1
                    continue
                signature = _load_signature(occurrence.file_path, max_frames=config.max_frames)
                if signature is None:
                    near_excluded["no_signature"] += 1
                else:
                    signatures[content_hash] = signature

            progress(0.85, "comparing media")
            ordered_hashes = sorted(signatures)
            stop = False
            for left_index, left_hash in enumerate(ordered_hashes):
                if stop:
                    break
                for right_hash in ordered_hashes[left_index + 1:]:
                    if pairs_compared >= config.max_candidate_pairs:
                        pair_limit_reached = True
                        stop = True
                        break
                    pairs_compared += 1
                    cancellation.check()
                    left = signatures[left_hash]
                    right = signatures[right_hash]
                    if (left.frame_count > 1) != (right.frame_count > 1):
                        near_excluded["animation"] += 1
                        continue
                    if left.frame_count != right.frame_count:
                        near_excluded["frame_count"] += 1
                        continue
                    aspect_delta = _aspect_delta(left.dimensions, right.dimensions)
                    if aspect_delta > _MAX_ASPECT_DELTA:
                        near_excluded["aspect"] += 1
                        continue
                    duration_delta = _duration_delta(left.duration_ms, right.duration_ms)
                    if duration_delta > _MAX_DURATION_DELTA:
                        near_excluded["duration"] += 1
                        continue
                    distance = max(
                        (left_frame ^ right_frame).bit_count()
                        for left_frame, right_frame in zip(left.frame_hashes, right.frame_hashes)
                    )
                    if distance > config.phash_threshold:
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
                        "score": round(1.0 - distance / 64.0, 4),
                        "aspect_delta": round(aspect_delta, 4),
                        "duration_delta": round(duration_delta, 4),
                        "frame_count": [left.frame_count, right.frame_count],
                        "caption_changed": captions_changed,
                    })
                    if len(candidates) >= config.max_candidate_pairs:
                        pair_limit_reached = True
                        stop = True
                        break
            if pair_limit_reached:
                diagnostics.append(f"near_pair_limit_reached:{config.max_candidate_pairs}")
                near_detection = "partial"
            if near_excluded["no_signature"]:
                diagnostics.append(f"near_signature_unavailable:{near_excluded['no_signature']}")
            if len(signatures) < len(groups):
                near_detection = "partial"
            if near_ready < len(occurrences) and near_detection == "enabled":
                near_detection = "partial"

        coverage = {
            "top_level_messages": len(snapshot.messages),
            "media_refs_seen": total_refs,
            "eligible": eligible,
            "analyzed": len(occurrences),
            "unique_media_groups": len(groups),
            "exact_clusters": len(clusters),
            "exact_occurrences": sum(cluster["occurrence_count"] for cluster in clusters),
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
            "near_excluded": near_excluded,
        }
        output = {
            "analyzer": self.analyzer_id,
            "version": self.version,
            "config": {
                "phash_threshold": config.phash_threshold,
                "phash_algorithm": "phash64-dct-8x8",
                "caption_source": "message_text",
                "near_detection": near_detection,
            },
            "coverage": coverage,
            "clusters": clusters,
            "candidates": candidates,
        }
        progress(0.95, "finalizing media duplicates")
        return AnalysisResult(
            output=output,
            evidence_ids=tuple(sorted(analyzed_evidence)),
            diagnostics=tuple(diagnostics),
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
