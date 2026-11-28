"""CL-06 objective/roast summary with chunk+reduce and grounding checks."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from analytics.analyzer import Cancellation
from analytics.models import Snapshot
from analytics.prompts import (
    CHUNK_SCHEMA,
    FACT_SCHEMA,
    STYLE_SCHEMA,
    build_chunk_messages,
    build_fact_messages,
    build_style_messages,
    prompt_hashes,
    prompt_versions,
)
from analytics.privacy import REDACTION_VERSION, redact_text
from analytics.providers.generation import (
    GenerationProvider,
    GenerationRequest,
    TokenUsage,
    estimate_tokens,
)

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CJK_NUMBER_RE = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿]+"
    r"(?=(?:次|条|个|人|天|年|月|日|周|小时|分钟|秒|张|段|句|页))"
)
_QUOTE_RE = re.compile(
    r"[\"“”「」『』‘’《》〈〉]"
    r"|(?<![A-Za-z0-9])'[^'\n]{1,200}'(?![A-Za-z0-9])"
)
_MAX_FACTS = 80


class SummaryError(RuntimeError):
    """Base class for summary composition failures."""


class SummaryInputError(SummaryError):
    """Raised when summary configuration or input is invalid."""


class SummaryGroundingError(SummaryError):
    """Raised when generated output cites unknown evidence/facts or invents numbers."""


@dataclass(frozen=True)
class SummaryConfig:
    max_chunk_tokens: int = 1200
    max_chunks: int = 20
    max_messages_per_chunk: int = 200
    max_message_chars: int = 500
    overlap_messages: int = 1
    max_output_tokens: int = 800
    style_max_output_tokens: int = 500
    temperature: float = 0.2
    redact_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value, minimum, maximum in (
            ("max_chunk_tokens", self.max_chunk_tokens, 64, 100_000),
            ("max_chunks", self.max_chunks, 1, 1000),
            ("max_messages_per_chunk", self.max_messages_per_chunk, 1, 1000),
            ("max_message_chars", self.max_message_chars, 1, 5000),
            ("overlap_messages", self.overlap_messages, 0, 10),
            ("max_output_tokens", self.max_output_tokens, 1, 100_000),
            ("style_max_output_tokens", self.style_max_output_tokens, 1, 100_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise SummaryInputError(f"{name} must be between {minimum} and {maximum}")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0.0 <= float(self.temperature) <= 2.0
        ):
            raise SummaryInputError("temperature must be between 0 and 2")
        if self.overlap_messages >= self.max_messages_per_chunk:
            raise SummaryInputError("overlap_messages must be smaller than max_messages_per_chunk")
        if isinstance(self.redact_terms, str) or not isinstance(self.redact_terms, Sequence):
            raise SummaryInputError("redact_terms must be a sequence of strings")
        cleaned_terms: list[str] = []
        for term in self.redact_terms:
            if not isinstance(term, str):
                raise SummaryInputError("redact_terms must contain only strings")
            stripped = term.strip()
            if not 2 <= len(stripped) <= 64:
                raise SummaryInputError("redact_terms entries must be 2..64 characters")
            if stripped not in cleaned_terms:
                cleaned_terms.append(stripped)
        if len(cleaned_terms) > 200:
            raise SummaryInputError("redact_terms must contain at most 200 entries")
        object.__setattr__(self, "redact_terms", tuple(cleaned_terms))


@dataclass(frozen=True)
class SummaryResult:
    objective: str
    roast: str
    topics: tuple[Mapping[str, Any], ...]
    claims: tuple[Mapping[str, Any], ...]
    facts: tuple[Mapping[str, Any], ...]
    evidence_ids: tuple[str, ...]
    chunk_summaries: tuple[Mapping[str, Any], ...]
    coverage: Mapping[str, Any]
    usage: TokenUsage
    diagnostics: tuple[str, ...]
    prompt_versions: Mapping[str, str]
    prompt_hashes: Mapping[str, str] = field(default_factory=dict)
    model_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "topics", tuple(MappingProxyType(dict(item)) for item in self.topics))
        object.__setattr__(self, "claims", tuple(MappingProxyType(dict(item)) for item in self.claims))
        object.__setattr__(self, "facts", tuple(MappingProxyType(dict(item)) for item in self.facts))
        object.__setattr__(
            self,
            "chunk_summaries",
            tuple(MappingProxyType(dict(item)) for item in self.chunk_summaries),
        )
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))
        object.__setattr__(self, "prompt_versions", MappingProxyType(dict(self.prompt_versions)))
        object.__setattr__(self, "prompt_hashes", MappingProxyType(dict(self.prompt_hashes)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "roast": self.roast,
            "topics": [dict(item) for item in self.topics],
            "claims": [dict(item) for item in self.claims],
            "facts": [dict(item) for item in self.facts],
            "evidence_ids": list(self.evidence_ids),
            "chunk_summaries": [dict(item) for item in self.chunk_summaries],
            "coverage": dict(self.coverage),
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "diagnostics": list(self.diagnostics),
            "prompt_versions": dict(self.prompt_versions),
            "prompt_hashes": dict(self.prompt_hashes),
            "model_ids": list(self.model_ids),
        }


def _add_fact(facts: list[tuple[str, Any]], label: str, value: Any) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        facts.append((label, value))
    elif isinstance(value, str) and value:
        facts.append((label, value))


def _flatten_facts(prefix: str, value: Any, facts: list[tuple[str, Any]]) -> None:
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            _flatten_facts(f"{prefix}.{key}", value[key], facts)
        return
    _add_fact(facts, prefix, value)


def _flatten_scalar_facts(prefix: str, value: Mapping, facts: list[tuple[str, Any]]) -> None:
    for key in sorted(value, key=str):
        child = value[key]
        if not isinstance(child, (Mapping, list, tuple)):
            _add_fact(facts, f"{prefix}.{key}", child)


def build_facts(
    snapshot: Snapshot,
    analyzer_outputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Build deterministic, numeric-only facts used to ground generated text."""
    entries: list[tuple[str, Any]] = []
    _add_fact(entries, "total_messages", len(snapshot.messages))
    range_start = snapshot.time_range.start.isoformat() if snapshot.time_range.start else "open"
    range_end = snapshot.time_range.end.isoformat() if snapshot.time_range.end else "open"
    _add_fact(entries, "time_range", f"{range_start}..{range_end}")
    _add_fact(entries, "timezone", snapshot.timezone)
    snapshot_coverage = snapshot.coverage.to_dict()
    core_coverage = {
        key: snapshot_coverage[key]
        for key in ("total_top_level", "forward_child_count", "earliest_at", "latest_at")
        if key in snapshot_coverage
    }
    _flatten_facts("snapshot_coverage", core_coverage, entries)

    analyzers: list[tuple[str, Mapping[str, Any]]] = []
    for analyzer_id in sorted(analyzer_outputs or {}):
        output = analyzer_outputs[analyzer_id]
        coverage = output.get("coverage") if isinstance(output, Mapping) else None
        if isinstance(coverage, Mapping):
            analyzers.append((analyzer_id, coverage))
    # Analyzer metrics (including duplicate counts) outrank detailed kind/date
    # breakdowns so the fact cap cannot silently remove the summary's core facts.
    for analyzer_id, coverage in analyzers:
        _flatten_scalar_facts(f"{analyzer_id}.coverage", coverage, entries)
    for analyzer_id, coverage in analyzers:
        for key in sorted(coverage, key=str):
            child = coverage[key]
            if isinstance(child, Mapping):
                _flatten_facts(f"{analyzer_id}.coverage.{key}", child, entries)
    _flatten_facts("snapshot_coverage.by_kind", snapshot_coverage.get("by_kind", {}), entries)
    _flatten_facts(
        "snapshot_coverage.media_by_kind",
        snapshot_coverage.get("media_by_kind", {}),
        entries,
    )

    omitted = max(0, len(entries) - _MAX_FACTS)
    facts: list[dict[str, Any]] = []
    for index, (label, value) in enumerate(entries[:_MAX_FACTS], start=1):
        facts.append({
            "id": f"F{index}",
            "label": label,
            "value": value,
            "text": f"{label} = {value}",
        })
    if omitted:
        facts.append({
            "id": f"F{len(facts) + 1}",
            "label": "facts_truncated",
            "value": omitted,
            "text": f"facts_truncated = {omitted}",
        })
    return tuple(facts)


def _allowed_numbers(facts: Sequence[Mapping[str, Any]]) -> set[str]:
    allowed: set[str] = set()
    for fact in facts:
        allowed.update(_NUMBER_RE.findall(str(fact["text"])))
    return allowed


def _assert_numbers(text: str, allowed: set[str], field_name: str) -> None:
    if _QUOTE_RE.search(text):
        raise SummaryGroundingError(f"{field_name} contains a quote; CL-06 does not support quotes")
    if _CJK_NUMBER_RE.search(text):
        raise SummaryGroundingError(f"{field_name} contains an unsupported number")
    for token in _NUMBER_RE.findall(text):
        if token not in allowed:
            raise SummaryGroundingError(f"{field_name} contains an unsupported number")


def _validate_evidence(ids: Any, allowed: set[str], field_name: str) -> None:
    if not isinstance(ids, list) or not ids:
        raise SummaryGroundingError(f"{field_name} must contain evidence IDs")
    for evidence_id in ids:
        if not isinstance(evidence_id, str) or evidence_id not in allowed:
            raise SummaryGroundingError(f"{field_name} contains an unknown evidence ID")


def _add_usage(total: TokenUsage, usage: TokenUsage) -> TokenUsage:
    return TokenUsage(
        total.input_tokens + usage.input_tokens,
        total.output_tokens + usage.output_tokens,
    )


class SummaryComposer:
    """Compose a bounded, grounded objective/roast summary from a snapshot."""

    def __init__(self, provider: GenerationProvider, config: SummaryConfig | None = None) -> None:
        if not hasattr(provider, "generate"):
            raise SummaryInputError("provider must implement generate")
        self.provider = provider
        self.config = config or SummaryConfig()

    def compose(
        self,
        snapshot: Snapshot,
        analyzer_outputs: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        cancellation: Cancellation | None = None,
        include_style: bool = True,
    ) -> SummaryResult:
        facts = build_facts(snapshot, analyzer_outputs)
        allowed_numbers = _allowed_numbers(facts)
        items, payload_coverage = self._collect_items(snapshot)
        duplicate_ids = self._duplicate_evidence_ids(analyzer_outputs)
        kept_items = [
            item for item in items
            if item["evidence_id"] not in duplicate_ids
        ]
        deduplicated = len(items) - len(kept_items)
        items = kept_items
        if not items:
            return SummaryResult(
                objective="所选时间范围内没有可分析的消息。",
                roast="",
                topics=(),
                claims=(),
                facts=facts,
                evidence_ids=(),
                chunk_summaries=(),
                coverage={
                    **payload_coverage,
                    "messages_available": 0,
                    "messages_included": 0,
                    "messages_truncated": 0,
                    "chunks_processed": 0,
                    "chunks_truncated": False,
                    "messages_deduplicated": deduplicated,
                    "redaction": REDACTION_VERSION,
                    "analyzer_outputs": sorted(analyzer_outputs or {}),
                    "prompt_versions": prompt_versions(),
                    "provider_calls": 0,
                },
                usage=TokenUsage(),
                diagnostics=("empty_snapshot",),
                prompt_versions=prompt_versions(),
                prompt_hashes=prompt_hashes(),
            )

        chunks, chunk_coverage = self._chunk_items(items)
        usage = TokenUsage()
        model_ids: list[str] = []
        chunk_summaries: list[dict[str, Any]] = []
        summarized_evidence: set[str] = set()
        for index, chunk in enumerate(chunks, start=1):
            if cancellation is not None:
                cancellation.check()
            response = self.provider.generate(GenerationRequest(
                messages=build_chunk_messages(facts, chunk),
                response_schema=CHUNK_SCHEMA,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                metadata={"stage": "chunk", "prompt_version": prompt_versions()["chunk"]},
            ), cancellation)
            usage = _add_usage(usage, response.usage)
            model_ids.append(response.model_id)
            allowed_evidence = self._chunk_evidence_ids(chunk)
            _validate_evidence(response.output.get("evidence_ids"), allowed_evidence, "chunk evidence")
            _assert_numbers(response.output["summary"], allowed_numbers, f"chunk {index} summary")
            summarized_evidence.update(response.output["evidence_ids"])
            chunk_summaries.append({
                "summary": response.output["summary"],
                "evidence_ids": list(response.output["evidence_ids"]),
            })

        if cancellation is not None:
            cancellation.check()
        fact_response = self.provider.generate(GenerationRequest(
            messages=build_fact_messages(facts, chunk_summaries),
            response_schema=FACT_SCHEMA,
            max_output_tokens=self.config.max_output_tokens,
            temperature=self.config.temperature,
            metadata={"stage": "facts", "prompt_version": prompt_versions()["fact"]},
        ), cancellation)
        usage = _add_usage(usage, fact_response.usage)
        model_ids.append(fact_response.model_id)
        fact_sheet = dict(fact_response.output)
        self._validate_fact_sheet(fact_sheet, summarized_evidence, facts)

        roast = ""
        if include_style:
            roast, style_usage, style_model = self.render_roast(
                facts, fact_sheet, cancellation=cancellation
            )
            usage = _add_usage(usage, style_usage)
            model_ids.append(style_model)
        diagnostics: list[str] = []
        if chunk_coverage["messages_truncated"]:
            diagnostics.append(f"messages_truncated:{chunk_coverage['messages_truncated']}")
        if chunk_coverage["chunks_truncated"]:
            diagnostics.append("chunks_truncated")
        if payload_coverage["messages_payload_truncated"]:
            diagnostics.append(
                f"messages_payload_truncated:{payload_coverage['messages_payload_truncated']}"
            )
        if payload_coverage["forward_truncated"]:
            diagnostics.append(f"forward_truncated:{payload_coverage['forward_truncated']}")
        if payload_coverage["replies_truncated"]:
            diagnostics.append(f"replies_truncated:{payload_coverage['replies_truncated']}")
        fact_truncated = next(
            (fact["value"] for fact in facts if fact["label"] == "facts_truncated"),
            0,
        )
        if fact_truncated:
            diagnostics.append(f"facts_truncated:{fact_truncated}")
        if not include_style:
            diagnostics.append("style_skipped")
        evidence_ids = sorted({
            evidence_id
            for item in fact_sheet.get("topics", [])
            for evidence_id in item.get("evidence_ids", [])
        } | {
            evidence_id
            for item in fact_sheet.get("claims", [])
            for evidence_id in item.get("evidence_ids", [])
        })
        coverage = {
            **payload_coverage,
            **chunk_coverage,
            "messages_deduplicated": deduplicated,
            "redaction": REDACTION_VERSION,
            "analyzer_outputs": sorted(analyzer_outputs or {}),
            "prompt_versions": prompt_versions(),
            "provider_calls": len(chunks) + 1 + (1 if include_style else 0),
        }
        return SummaryResult(
            objective=fact_sheet["objective"],
            roast=roast,
            topics=tuple(fact_sheet["topics"]),
            claims=tuple(fact_sheet["claims"]),
            facts=facts,
            evidence_ids=tuple(evidence_ids),
            chunk_summaries=tuple(chunk_summaries),
            coverage=coverage,
            usage=usage,
            diagnostics=tuple(diagnostics),
            prompt_versions=prompt_versions(),
            prompt_hashes=prompt_hashes(),
            model_ids=tuple(dict.fromkeys(model_ids)),
        )

    def render_roast(
        self,
        facts: Sequence[Mapping[str, Any]],
        fact_sheet: Mapping[str, Any],
        *,
        cancellation: Cancellation | None = None,
    ) -> tuple[str, TokenUsage, str]:
        if cancellation is not None:
            cancellation.check()
        response = self.provider.generate(GenerationRequest(
            messages=build_style_messages(facts, fact_sheet),
            response_schema=STYLE_SCHEMA,
            max_output_tokens=self.config.style_max_output_tokens,
            temperature=self.config.temperature,
            metadata={"stage": "style", "prompt_version": prompt_versions()["style"]},
        ), cancellation)
        roast = response.output["roast"]
        _assert_numbers(roast, _allowed_numbers(facts), "roast")
        return roast, response.usage, response.model_id

    @staticmethod
    def _duplicate_evidence_ids(
        analyzer_outputs: Mapping[str, Mapping[str, Any]] | None,
    ) -> set[str]:
        duplicate_ids: set[str] = set()
        for output in (analyzer_outputs or {}).values():
            for key in ("clusters", "short_phrase_clusters"):
                clusters = output.get(key) if isinstance(output, Mapping) else None
                if not isinstance(clusters, list):
                    continue
                for cluster in clusters:
                    if not isinstance(cluster, Mapping):
                        continue
                    representative = cluster.get("representative_evidence_id")
                    members = cluster.get("member_evidence_ids", [])
                    if not isinstance(members, list):
                        continue
                    for member in members:
                        if isinstance(member, str) and member != representative:
                            duplicate_ids.add(member)
        return duplicate_ids

    def _collect_items(
        self,
        snapshot: Snapshot,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        items: list[dict[str, Any]] = []
        coverage = {
            "messages_payload_truncated": 0,
            "forward_truncated": 0,
            "replies_truncated": 0,
        }

        def render_text(text: str) -> tuple[str, bool]:
            rendered = redact_text(
                text,
                max_chars=self.config.max_message_chars + 1,
                terms=self.config.redact_terms,
            )
            truncated = len(rendered) > self.config.max_message_chars
            return rendered[:self.config.max_message_chars], truncated

        def render_reply(reply: Any | None) -> tuple[dict[str, Any] | None, bool]:
            if reply is None:
                return None, False
            text, truncated = render_text(reply.text_excerpt)
            payload: dict[str, Any] = {
                "sender": reply.sender_pseudonym,
                "kind": reply.kind,
                "text": text,
            }
            if reply.target_message_id:
                payload["target_evidence_id"] = reply.target_message_id
            if truncated:
                payload["truncated"] = True
            return payload, truncated

        def add(
            evidence_id: str,
            text: str,
            sender: str,
            sent_at: datetime,
            kind: str,
            *,
            has_link: bool,
            has_media: bool,
            reply_to: Any | None = None,
            forward_truncated: bool = False,
        ) -> None:
            rendered_text, text_truncated = render_text(text)
            reply_payload, reply_truncated = render_reply(reply_to)
            if text_truncated or reply_truncated:
                coverage["messages_payload_truncated"] += 1
            if reply_truncated:
                coverage["replies_truncated"] += 1
            if forward_truncated:
                coverage["forward_truncated"] += 1
            markers = []
            if has_link:
                markers.append("[链接]")
            if has_media:
                markers.append("[媒体]")
            rendered = " ".join(part for part in (rendered_text, *markers) if part)
            if not rendered and reply_payload is None:
                return
            item: dict[str, Any] = {
                "evidence_id": evidence_id,
                "sent_at": sent_at.astimezone(timezone.utc).isoformat(),
                "sender": sender,
                "kind": kind,
                "text": rendered,
            }
            if reply_payload is not None:
                item["reply_to"] = reply_payload
            if text_truncated:
                item["text_truncated"] = True
            if reply_truncated:
                item["reply_truncated"] = True
            if forward_truncated:
                item["forward_truncated"] = True
            items.append(item)

        def visit_child(child, fallback_sent_at: datetime) -> None:
            sent_at = child.sent_at or fallback_sent_at
            add(
                child.child_id,
                child.text,
                child.sender_pseudonym,
                sent_at,
                child.kind,
                has_link=child.link_meta is not None,
                has_media=bool(child.media_refs),
                reply_to=child.reply_to,
                forward_truncated=child.truncated,
            )
            for nested in child.forward_children:
                visit_child(nested, sent_at)

        for message in snapshot.messages:
            forward_only_parent = (
                message.kind == "chatrecord"
                and bool(message.forward_children)
                and message.link_meta is None
                and not message.media_refs
                and message.reply_to is None
            )
            if forward_only_parent and message.forward_truncated:
                coverage["forward_truncated"] += 1
            if not forward_only_parent:
                add(
                    message.message_id,
                    message.text,
                    message.sender_pseudonym,
                    message.sent_at,
                    message.kind,
                    has_link=message.link_meta is not None,
                    has_media=bool(message.media_refs),
                    reply_to=message.reply_to,
                    forward_truncated=message.forward_truncated,
                )
            for child in message.forward_children:
                visit_child(child, message.sent_at)
        return items, coverage

    @staticmethod
    def _chunk_evidence_ids(chunk: Sequence[Mapping[str, Any]]) -> set[str]:
        evidence: set[str] = set()
        for item in chunk:
            evidence_id = item.get("evidence_id")
            if isinstance(evidence_id, str):
                evidence.add(evidence_id)
            reply = item.get("reply_to")
            if isinstance(reply, Mapping):
                target = reply.get("target_evidence_id")
                if isinstance(target, str):
                    evidence.add(target)
        return evidence

    def _chunk_items(
        self,
        items: Sequence[Mapping[str, Any]],
    ) -> tuple[list[list[Mapping[str, Any]]], dict[str, Any]]:
        available_chunks: list[list[Mapping[str, Any]]] = []
        start = 0
        while start < len(items):
            chunk: list[Mapping[str, Any]] = []
            tokens = 0
            index = start
            while index < len(items) and len(chunk) < self.config.max_messages_per_chunk:
                item = items[index]
                item_tokens = max(1, estimate_tokens(json.dumps(item, ensure_ascii=False)))
                if chunk and tokens + item_tokens > self.config.max_chunk_tokens:
                    break
                chunk.append(item)
                tokens += item_tokens
                index += 1
            if not chunk:
                chunk = [items[start]]
                index = start + 1
            available_chunks.append(chunk)
            if index >= len(items):
                break
            start = max(start + 1, index - self.config.overlap_messages)

        if len(available_chunks) <= self.config.max_chunks:
            chunks = available_chunks
        elif self.config.max_chunks == 1:
            chunks = [available_chunks[0]]
        else:
            # Sample evenly across the full time span instead of always taking
            # an earliest-message prefix; the first/last chunks stay visible.
            last = len(available_chunks) - 1
            indexes = {
                round(index * last / (self.config.max_chunks - 1))
                for index in range(self.config.max_chunks)
            }
            chunks = [available_chunks[index] for index in sorted(indexes)]
        included: set[str] = set()
        for chunk in chunks:
            included.update(item["evidence_id"] for item in chunk)
        coverage = {
            "messages_available": len(items),
            "messages_included": len(included),
            "messages_truncated": len(items) - len(included),
            "chunks_available": len(available_chunks),
            "chunks_processed": len(chunks),
            "chunks_truncated": len(chunks) < len(available_chunks),
            "max_chunks": self.config.max_chunks,
            "max_chunk_tokens": self.config.max_chunk_tokens,
        }
        return chunks, coverage

    def _validate_fact_sheet(
        self,
        fact_sheet: Mapping[str, Any],
        allowed_evidence: set[str],
        facts: Sequence[Mapping[str, Any]],
    ) -> None:
        fact_text = {str(fact["id"]): str(fact["text"]) for fact in facts}
        allowed_numbers = _allowed_numbers(facts)
        _assert_numbers(fact_sheet["objective"], allowed_numbers, "objective")
        for topic in fact_sheet["topics"]:
            _validate_evidence(topic.get("evidence_ids"), allowed_evidence, "topic evidence")
            _assert_numbers(topic["label"], allowed_numbers, "topic label")
        for claim in fact_sheet["claims"]:
            _validate_evidence(claim.get("evidence_ids"), allowed_evidence, "claim evidence")
            fact_ids = claim.get("fact_ids")
            if not isinstance(fact_ids, list) or not fact_ids or any(
                not isinstance(fact_id, str) or fact_id not in fact_text
                for fact_id in fact_ids
            ):
                raise SummaryGroundingError("claim contains an unknown fact ID")
            claim_numbers: set[str] = set()
            for fact_id in fact_ids:
                claim_numbers.update(_NUMBER_RE.findall(fact_text[fact_id]))
            _assert_numbers(claim["text"], claim_numbers, "claim text")
