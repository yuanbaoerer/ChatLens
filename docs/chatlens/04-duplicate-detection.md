# Duplicate detection

L1: SHA-256 bytes for exact media; conservative normalized text hash (Unicode normalization, whitespace; preserve negation, numbers, punctuation where meaningful); canonical URL with known tracking params only, preserve meaningful query/fragment and original. Link title alone not sufficient. Blank placeholders and missing media are not duplicates. Repeated short conversational phrases counted separately from informational redundancy.

L2: static pHash candidate matching with configurable Hamming threshold; use dimension/aspect/visual checks and OCR to detect changed captions. GIF: exact hash and sampled frame hashes with sequence/duration checks; do not collapse animations based on first frame. Different captions/template variants separate. Treat uncertain perceptual matches as candidates, not automatically equivalent.

L3: local text embedding Top-K retrieval, bounded candidates and time window configurable; OCR/image embedding as needed. Evaluate recall including distant repeats. Jev adjudicates semantic equality, same event, new fact and contradiction. No all-pairs remote inference. Exact deterministic matches can bypass model.

Relations: EXACT_DUPLICATE, NEAR_DUPLICATE, SEMANTIC_DUPLICATE, SAME_MEME_TEMPLATE, SAME_EVENT_WITH_UPDATE, CONTRADICTORY, UNRELATED, UNCERTAIN. Direction of new information matters: newer contains claims absent from older; same topic != same event. Preserve discussion context.

Clusters: exact equivalence grouped by key; approximate semantic joins checked against representative and conflict/consistency rules; do not transitively merge similarity edges indiscriminately. Retain edges, rejected/uncertain candidates and counts. Test A~B~C bridge cases, URL variants, changed number/negation, crop/resize, GIF change and missing files.
