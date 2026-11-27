# Duplicate detection

L1: SHA-256 bytes for exact media; conservative normalized text hash (Unicode normalization, whitespace; preserve negation, numbers, punctuation where meaningful); canonical URL with known tracking params only, preserve meaningful query/fragment and original. Link title alone not sufficient. Blank placeholders and missing media are not duplicates. Repeated short conversational phrases counted separately from informational redundancy.

L2: static pHash candidate matching with configurable Hamming threshold; use dimension/aspect/visual checks and OCR to detect changed captions. GIF: exact hash and sampled frame hashes with sequence/duration checks; do not collapse animations based on first frame. Different captions/template variants separate. Treat uncertain perceptual matches as candidates, not automatically equivalent.

L3: local text embedding Top-K retrieval, bounded candidates and time window configurable; OCR/image embedding as needed. Evaluate recall including distant repeats. Jev adjudicates semantic equality, same event, new fact and contradiction. No all-pairs remote inference. Exact deterministic matches can bypass model.

Relations: EXACT_DUPLICATE, NEAR_DUPLICATE, SEMANTIC_DUPLICATE, SAME_MEME_TEMPLATE, SAME_EVENT_WITH_UPDATE, CONTRADICTORY, UNRELATED, UNCERTAIN. Direction of new information matters: newer contains claims absent from older; same topic != same event. Preserve discussion context.

Clusters: exact equivalence grouped by key; approximate semantic joins checked against representative and conflict/consistency rules; do not transitively merge similarity edges indiscriminately. Retain edges, rejected/uncertain candidates and counts. Test A~B~C bridge cases, URL variants, changed number/negation, crop/resize, GIF change and missing files.

## CL-03 deterministic implementation (media only)

`media.duplicates` v1 scans top-level messages and nested forward children:

- Exact: readable, non-thumbnail image/emoji/sticker media grouped by SHA-256; when `media_root` is supplied and the file is available, hashes are re-derived from current bytes, otherwise valid snapshot hashes are used. Without `media_root`, post-import file changes cannot be detected. Groups with at least two occurrences and one normalized caption become `exact_duplicate` clusters; caption changes become `same_meme_template` clusters.
- Near: configurable 63-bit AC DCT pHash (`phash_threshold` default 10, 0..63), aspect checks, and GIF/WebP frame-count/sample-sequence/duration checks. Animated media is never compared by its first frame alone; static-vs-animated is excluded.
- Captions: normalized message text (NFKC, whitespace, placeholder labels) distinguishes `near_duplicate` from `same_meme_template`; output emits `caption_variant_count`, not caption hashes. OCR is not part of CL-03; the output records `caption_source: "message_text"`.
- Excluded: unsupported-kind, missing, unsupported, failed, not-requested, thumbnail, no-path, invalid-hash, outside-root and unreadable assets are counted separately and never treated as unique or duplicate evidence. Coverage includes per-kind counters, `hash_mismatch` when current bytes differ from the snapshot hash, and `confirmed_duplicate_occurrences = sum(exact cluster size - 1)`.
- Config: `media_root` (optional absolute approved root, required for near detection), `phash_threshold`, `max_candidate_pairs`, `max_near_groups`, `max_frames`.
- Output: `coverage` (including `by_kind`), `clusters` (exact/template), `candidates` (near/template/uncertain), per-stage `evidence_ids`; near/template/uncertain candidates are not auto-merged into exact clusters.
