"""Deterministic exact duplicate detection for text, links and forward content.

Text is normalized with NFKC and collapsed whitespace while preserving case,
numbers, negation and punctuation. Links are grouped by canonical http(s) URL
with only known tracking parameters removed; titles alone are never a key.
Short conversational phrases are emitted separately from informational text.

Configuration (``AnalysisContext.config``):
- ``short_text_max_chars``: 1..1000, default 12.
- ``max_url_length``: 1..100000, default 4096.
- ``max_clusters`` / ``max_member_ids`` / ``max_evidence_ids``: bounded output.
"""
from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from analytics.analyzer import (
    AnalysisContext,
    AnalysisResult,
    Analyzer,
    Cancellation,
    ProgressReporter,
)
from analytics.models import ForwardChild, LinkMeta, Snapshot

ANALYZER_ID = "text.links.duplicates"
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid",
    "mc_cid", "mc_eid", "mkt_tok", "igshid", "si", "spm", "scm",
    "_hsenc", "_hsmi",
})
_PLACEHOLDER_TEXT = frozenset({
    "[图片]", "[表情]", "[动画表情]", "[链接]", "[视频]", "[语音]", "[文件]",
    "[位置]", "[名片]", "[红包]", "[转账]", "[小程序]", "[聊天记录]", "[音乐]",
})
_KIND_STAT_KEYS = (
    "owners",
    "eligible",
    "analyzed",
    "blank",
    "placeholder",
    "short_occurrences",
    "long_occurrences",
    "missing_url",
    "unsupported_scheme",
    "userinfo",
    "url_too_long",
    "invalid_url",
    "host_missing",
    "clusters",
    "short_clusters",
    "duplicate_occurrences",
    "short_duplicate_occurrences",
)


def _bounded_int(raw: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class _Config:
    short_text_max_chars: int = 12
    max_url_length: int = 4096
    max_clusters: int = 1000
    max_member_ids: int = 1000
    max_evidence_ids: int = 5000

    @classmethod
    def from_context(cls, context: AnalysisContext) -> "_Config":
        raw = context.config
        return cls(
            short_text_max_chars=_bounded_int(raw, "short_text_max_chars", 12, 1, 1000),
            max_url_length=_bounded_int(raw, "max_url_length", 4096, 1, 100_000),
            max_clusters=_bounded_int(raw, "max_clusters", 1000, 1, 100_000),
            max_member_ids=_bounded_int(raw, "max_member_ids", 1000, 1, 100_000),
            max_evidence_ids=_bounded_int(raw, "max_evidence_ids", 5000, 1, 100_000),
        )


@dataclass(frozen=True)
class _Owner:
    evidence_id: str
    text: str
    sender: str
    sent_at: datetime
    kind: str
    link_meta: LinkMeta | None
    children: tuple[ForwardChild, ...]


@dataclass(frozen=True)
class _TextOccurrence:
    evidence_id: str
    text: str
    sender: str
    sent_at: datetime
    short_form: bool


@dataclass(frozen=True)
class _LinkOccurrence:
    evidence_id: str
    canonical_url: str
    host: str
    title: str
    sender: str
    sent_at: datetime


def _new_kind_stats() -> dict[str, int]:
    return {key: 0 for key in _KIND_STAT_KEYS}


def _normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).split())


def _normalize_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(title or "")).split())


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith("utm_")


def _canonicalize_url(raw: str, max_length: int) -> tuple[str | None, str | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, None, "missing_url"
    if len(text) > max_length:
        return None, None, "url_too_long"
    if any(ord(character) < 32 for character in text):
        return None, None, "invalid_url"
    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            return None, None, "unsupported_scheme"
        if parsed.username is not None or parsed.password is not None:
            return None, None, "userinfo"
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None, None, "invalid_url"
    if not host:
        return None, None, "host_missing"
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    host = host.lower()
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port is not None:
        netloc += f":{port}"
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError:
        return None, None, "invalid_url"
    query = urlencode(
        [(name, value) for name, value in pairs if not _is_tracking_param(name)],
        doseq=True,
    )
    canonical = urlunsplit((scheme, netloc, parsed.path or "/", query, parsed.fragment))
    return canonical, host, None


def _cluster_id(prefix: str, value: str, snapshot: Snapshot) -> str:
    secret = f"{ANALYZER_ID}:{snapshot.snapshot_id}".encode("utf-8")
    digest = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _iter_owners(snapshot: Snapshot) -> tuple[list[_Owner], int]:
    owners: list[_Owner] = []
    stack = [
        _Owner(
            evidence_id=message.message_id,
            text=message.text,
            sender=message.sender_pseudonym,
            sent_at=message.sent_at,
            kind=message.kind,
            link_meta=message.link_meta,
            children=message.forward_children,
        )
        for message in reversed(snapshot.messages)
    ]
    while stack:
        owner = stack.pop()
        owners.append(owner)
        for child in reversed(owner.children):
            stack.append(_Owner(
                evidence_id=child.child_id,
                text=child.text,
                sender=child.sender_pseudonym,
                sent_at=child.sent_at or owner.sent_at,
                kind=child.kind,
                link_meta=child.link_meta,
                children=child.forward_children,
            ))
    return owners, len(snapshot.messages)


def _text_cluster(
    group: list[_TextOccurrence],
    prefix: str,
    snapshot: Snapshot,
    max_member_ids: int,
) -> dict[str, Any]:
    group = sorted(group, key=lambda item: (item.sent_at, item.evidence_id))
    evidence_ids = sorted({item.evidence_id for item in group})
    return {
        "cluster_id": _cluster_id(prefix, group[0].text, snapshot),
        "content_kind": "text",
        "relation": "exact_duplicate",
        "representative_evidence_id": group[0].evidence_id,
        "member_evidence_ids": evidence_ids[:max_member_ids],
        "member_ids_truncated": len(evidence_ids) > max_member_ids,
        "occurrence_count": len(group),
        "unique_sender_count": len({item.sender for item in group}),
        "first_seen": _iso(group[0].sent_at),
        "last_seen": _iso(group[-1].sent_at),
        "text_length": len(group[0].text),
        "short_form": group[0].short_form,
    }


def _link_cluster(
    group: list[_LinkOccurrence],
    snapshot: Snapshot,
    max_member_ids: int,
) -> dict[str, Any]:
    group = sorted(group, key=lambda item: (item.sent_at, item.evidence_id))
    evidence_ids = sorted({item.evidence_id for item in group})
    return {
        "cluster_id": _cluster_id("link", group[0].canonical_url, snapshot),
        "content_kind": "link",
        "relation": "exact_duplicate",
        "representative_evidence_id": group[0].evidence_id,
        "member_evidence_ids": evidence_ids[:max_member_ids],
        "member_ids_truncated": len(evidence_ids) > max_member_ids,
        "occurrence_count": len(group),
        "unique_sender_count": len({item.sender for item in group}),
        "first_seen": _iso(group[0].sent_at),
        "last_seen": _iso(group[-1].sent_at),
        "canonical_url": group[0].canonical_url,
        "host": group[0].host,
        "title_variant_count": len({item.title for item in group}),
    }


class TextLinkDuplicateAnalyzer(Analyzer):
    """Exact text/link duplicates with conservative normalization and URL rules."""

    analyzer_id = ANALYZER_ID
    version = "1"
    supported_message_types = frozenset()
    estimated_workload = 2

    def run(
        self,
        snapshot: Snapshot,
        context: AnalysisContext,
        cancellation: Cancellation,
        progress: ProgressReporter,
    ) -> AnalysisResult:
        config = _Config.from_context(context)
        owners, top_level_messages = _iter_owners(snapshot)
        by_kind = {"text": _new_kind_stats(), "link": _new_kind_stats()}
        text_occurrences: list[_TextOccurrence] = []
        link_occurrences: list[_LinkOccurrence] = []
        analyzed_evidence: set[str] = set()
        text_eligible = 0
        text_analyzed = 0
        text_blank = 0
        text_placeholder = 0
        link_eligible = 0
        link_analyzed = 0

        for index, owner in enumerate(owners):
            cancellation.check()
            if index % 64 == 0 and owners:
                progress(min(0.45, 0.45 * index / len(owners)), "scanning text and links")
            is_link = owner.link_meta is not None or owner.kind in ("link", "miniprogram")
            if is_link:
                stats = by_kind["link"]
                stats["owners"] += 1
                link_eligible += 1
                stats["eligible"] += 1
                link_meta = owner.link_meta
                canonical, host, reason = _canonicalize_url(
                    link_meta.url if link_meta is not None else "",
                    config.max_url_length,
                )
                if reason is not None:
                    stats[reason] += 1
                    continue
                assert canonical is not None and host is not None
                link_analyzed += 1
                stats["analyzed"] += 1
                analyzed_evidence.add(owner.evidence_id)
                link_occurrences.append(_LinkOccurrence(
                    evidence_id=owner.evidence_id,
                    canonical_url=canonical,
                    host=host,
                    title=_normalize_title(link_meta.title if link_meta is not None else ""),
                    sender=owner.sender,
                    sent_at=owner.sent_at,
                ))
                continue
            if owner.kind != "text":
                continue
            stats = by_kind["text"]
            stats["owners"] += 1
            text_eligible += 1
            stats["eligible"] += 1
            normalized = _normalize_text(owner.text)
            if not normalized:
                text_blank += 1
                stats["blank"] += 1
                continue
            if normalized in _PLACEHOLDER_TEXT:
                text_placeholder += 1
                stats["placeholder"] += 1
                continue
            text_analyzed += 1
            stats["analyzed"] += 1
            short_form = len(normalized) <= config.short_text_max_chars
            stats["short_occurrences" if short_form else "long_occurrences"] += 1
            analyzed_evidence.add(owner.evidence_id)
            text_occurrences.append(_TextOccurrence(
                evidence_id=owner.evidence_id,
                text=normalized,
                sender=owner.sender,
                sent_at=owner.sent_at,
                short_form=short_form,
            ))

        progress(0.55, "grouping text and links")
        text_groups: dict[str, list[_TextOccurrence]] = {}
        for occurrence in text_occurrences:
            text_groups.setdefault(occurrence.text, []).append(occurrence)
        link_groups: dict[str, list[_LinkOccurrence]] = {}
        for occurrence in link_occurrences:
            link_groups.setdefault(occurrence.canonical_url, []).append(occurrence)

        clusters: list[dict[str, Any]] = []
        short_phrase_clusters: list[dict[str, Any]] = []
        clusters_truncated = 0
        short_clusters_truncated = 0
        text_cluster_count = 0
        short_cluster_count = 0
        link_cluster_count = 0
        text_duplicate_occurrences = 0
        short_duplicate_occurrences = 0
        link_duplicate_occurrences = 0

        short_occurrences = sum(1 for item in text_occurrences if item.short_form)
        for text in sorted(text_groups, key=lambda value: _cluster_id("text", value, snapshot)):
            group = text_groups[text]
            if len(group) < 2:
                continue
            short_form = group[0].short_form
            if short_form:
                short_cluster_count += 1
                short_duplicate_occurrences += len(group) - 1
                by_kind["text"]["short_clusters"] += 1
                by_kind["text"]["short_duplicate_occurrences"] += len(group) - 1
                if len(short_phrase_clusters) >= config.max_clusters:
                    short_clusters_truncated += 1
                    continue
                short_phrase_clusters.append(
                    _text_cluster(group, "short", snapshot, config.max_member_ids)
                )
            else:
                text_cluster_count += 1
                text_duplicate_occurrences += len(group) - 1
                by_kind["text"]["clusters"] += 1
                by_kind["text"]["duplicate_occurrences"] += len(group) - 1
                if len(clusters) >= config.max_clusters:
                    clusters_truncated += 1
                    continue
                clusters.append(_text_cluster(group, "text", snapshot, config.max_member_ids))

        for canonical_url in sorted(link_groups):
            group = link_groups[canonical_url]
            if len(group) < 2:
                continue
            link_cluster_count += 1
            link_duplicate_occurrences += len(group) - 1
            by_kind["link"]["clusters"] += 1
            by_kind["link"]["duplicate_occurrences"] += len(group) - 1
            if len(clusters) >= config.max_clusters:
                clusters_truncated += 1
                continue
            clusters.append(_link_cluster(group, snapshot, config.max_member_ids))

        sorted_evidence = sorted(analyzed_evidence)
        evidence_truncated = len(sorted_evidence) > config.max_evidence_ids
        diagnostics: list[str] = []
        if clusters_truncated:
            diagnostics.append(f"clusters_truncated:{clusters_truncated}")
        if short_clusters_truncated:
            diagnostics.append(f"short_phrase_clusters_truncated:{short_clusters_truncated}")
        if evidence_truncated:
            diagnostics.append(f"evidence_ids_truncated:{len(sorted_evidence) - config.max_evidence_ids}")

        coverage = {
            "top_level_messages": top_level_messages,
            "owners_scanned": len(owners),
            "text_eligible": text_eligible,
            "text_analyzed": text_analyzed,
            "text_blank": text_blank,
            "text_placeholder": text_placeholder,
            "short_phrase_occurrences": short_occurrences,
            "short_phrase_clusters": short_cluster_count,
            "short_phrase_duplicate_occurrences": short_duplicate_occurrences,
            "text_clusters": text_cluster_count,
            "text_duplicate_occurrences": text_duplicate_occurrences,
            "link_eligible": link_eligible,
            "link_analyzed": link_analyzed,
            "link_missing_url": by_kind["link"]["missing_url"],
            "link_unsupported_scheme": by_kind["link"]["unsupported_scheme"],
            "link_userinfo": by_kind["link"]["userinfo"],
            "link_url_too_long": by_kind["link"]["url_too_long"],
            "link_invalid_url": by_kind["link"]["invalid_url"],
            "link_host_missing": by_kind["link"]["host_missing"],
            "link_clusters": link_cluster_count,
            "link_duplicate_occurrences": link_duplicate_occurrences,
            "confirmed_duplicate_occurrences": text_duplicate_occurrences + link_duplicate_occurrences,
            "by_kind": by_kind,
            "clusters_returned": len(clusters),
            "clusters_truncated": clusters_truncated,
            "short_phrase_clusters_returned": len(short_phrase_clusters),
            "short_phrase_clusters_truncated": short_clusters_truncated,
            "evidence_ids_returned": min(len(sorted_evidence), config.max_evidence_ids),
            "evidence_ids_truncated": evidence_truncated,
        }
        output = {
            "analyzer": self.analyzer_id,
            "version": self.version,
            "config": {
                "normalization": "nfkc-whitespace-v1",
                "canonicalization": "tracking-params-v1",
                "short_text_max_chars": config.short_text_max_chars,
                "max_url_length": config.max_url_length,
                "max_clusters": config.max_clusters,
                "max_member_ids": config.max_member_ids,
                "max_evidence_ids": config.max_evidence_ids,
            },
            "coverage": coverage,
            "clusters": clusters,
            "short_phrase_clusters": short_phrase_clusters,
            "candidates": [],
            "rejected": [],
        }
        progress(0.95, "finalizing text and link duplicates")
        return AnalysisResult(
            output=output,
            evidence_ids=tuple(sorted_evidence[:config.max_evidence_ids]),
            diagnostics=tuple(diagnostics),
        )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
