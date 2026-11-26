# Architecture

Moect existing Windows data access and media/export services → MoectAdapter or JSONL+media fallback → normalized immutable snapshot → analysis application service → deterministic detectors + decision/embedding providers + generation analyzers → evidence-backed report → existing frontend/bridge.

README baseline at 993f4e8: Python 3.11, pywebview/WebView2, React/TypeScript, existing npm/build/installer. Actual service, bridge and task registration must be mapped in Phase 0; no assumption that alternative Qt entry points are primary.

Proposed analytics/ modules: adapters, models, preprocessing, analyzers, providers/{decision,generation,embedding}, clustering, reporting, storage, service, prompts, pipeline. Use existing task scheduling if safe; otherwise one bounded background worker with cancellation/progress. No new backend server. GUI calls typed service entry points, never implements detector logic.

Analyzer contract: id, version, prerequisites, supported types, estimated workload, run(snapshot, context, cancellation, progress) → typed result + evidence + diagnostics. Static registry is sufficient. Dependency DAG: coverage + stats + exact/near detection; retrieval → Jev; context construction → LLM; composer consumes available outputs. LLM can run without Jev, but distinguish incomplete analysis. Persist run manifest, provider/model/prompt/config version and stage status to local SQLite; no separate DB service. Keep artifacts scoped to a run. Cache by content/model/prompt/config version only where privacy allows; cancellation must not mark success.
