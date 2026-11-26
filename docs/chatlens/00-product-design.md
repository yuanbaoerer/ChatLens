# ChetLens — product definition

Status: proposed MVP. Repository name follows the existing fork ChetLens; ChatLens was the discussion codename. Upstream Moect is the data acquisition and desktop foundation. The user has manually confirmed Moect works on Windows; coverage of individual message/media types is not yet measured.

## User journey
Open desktop application → connect via existing Moect workflow or load authorized local export → select one group and date range (7 days, 30 days, custom) → preview coverage and select analyzers → analyze → view report and drill down to original messages → export a redacted HTML/JSON report.

## P0
- Repeat static images, GIF/stickers, text, links/forwarded articles, and semantic duplicate detection with Jev.
- Distinguish same-event updates and same-template different captions from actual duplicates.
- Objective group summary and humorous/roast summary using ordinary LLM; hot topics and activity statistics; evidence-based report with repeat counts and timeline.
- Simple integrated GUI, configurable decision/generation/embedding providers, opt-in model calls, local storage and explicit incomplete results.
- Windows 10/11 x64 first; no GPU required.

P1: quotes, more report styles, incremental caching/sync, advanced meme template detection, periodic reports. Non-goals: realtime monitoring, automatic replies, multi-platform, cross-group tracking, user accounts, servers, plugin ecosystem, agent orchestration, individual personality profiling.

Success: authorized user can select one real group and run a report without manual JSON manipulation in GUI. Offline deterministic results remain usable without any model. Complete report includes configured Jev and LLM results or clearly labels failure/disabled status. Repeated memes are behavior/frequency, not inherently low value.
