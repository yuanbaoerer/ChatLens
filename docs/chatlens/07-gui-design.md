# GUI and report

Reuse current React/TypeScript + pywebview frontend, design consistent with Moect theme and layout. Add group context action or analytics page; Phase 0 identify exact bridge and component seams. No second GUI or HTTP server.

Flow: select existing group → date range → coverage preview (messages/media, missing types) → analyzers toggles (basic, Jev, objective/roast, topics/activity) → model configuration + consent preview (destination, data categories, estimated limit) → start → progress by stage, processed/total, failures, cancel → report. Do not block UI thread, no fake progress. Read-only analysis.

Report sections: coverage; overall and per-type repetition with denominator; high-frequency memes with thumbnail, counts and timestamps; duplicate forwarding clusters and same-event updates; activity time distribution; topics; objective and roast tabs; evidence drill-down to original local records, uncertainty/partial coverage. Clearly show unavailable data and provider failures. HTML/JSON export with explicit redaction preview, no external assets or active untrusted markup; escape text/URLs. Default exclude identifiable member data and raw images from shareable export. Keep reports local; delete run data from analytics storage without touching WeChat/Moect exports.

Acceptance: Windows desktop demo without terminal, authorized real group with one-click analysis after connection; offline fallback, cancellation, missing media and failed model request tested. Preserve upstream screens and export behavior.
