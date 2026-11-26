# Moect integration

Phase 0: record upstream commit, Windows/WeChat version, existing build commands and actual UI entry, bridge methods, group list/pagination, date semantics, message and media resolver, export schema, cancellation, cache and updater. Trace real code with file/line references. Preserve upstream behavior. Confirm read-only data contract and no automatic fetching from remote sources without consent. Reuse existing supported local acquisition; do not extend credential extraction, access protection bypass or security-disable instructions.

Preferred adapter wraps existing service or export data contract. Fallback reads authorized JSONL/JSON with media directory; offline tests must work without WeChat. No direct analytics dependency on private WeChat tables. Group IDs and stable message IDs (or deterministic source-specific fallback) scoped to account/source; stable paging, dedup import, timezone-aware half-open range. Support nested forwarded records and quotes without counting children as new top-level sends.

Media statuses: readable, missing, unsupported, failed, not_requested. Resolve paths only within approved root; no arbitrary filesystem reads. Distinguish actual sticker/GIF bytes from placeholder or thumbnail. No silent remote download. Report counts and date coverage by type; never infer missing media are unique.

Acceptance: authorized sample with ~500 texts, ~100 image/sticker records and forward examples if available; actual coverage with unavailable types explicitly logged, IDs stable across rerun, no writes to source. Synthetic fixtures committed, real files never committed. User already verified basic Moect operation, not completeness.
