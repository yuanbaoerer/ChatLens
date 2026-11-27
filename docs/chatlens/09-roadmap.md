# Roadmap (proposed)

Each task uses focused PR, tests, evidence, no auto-merge. Dependencies are logical task IDs, not assumed GitHub issue numbers.

## Implementation status

| ID | Repository status | Verification |
|---|---|---|
| CL-00 | Audit and hybrid development guide merged in [PR #2](https://github.com/yuanbaoerer/ChatLens/pull/2) (`720af50`). | WSL audit and synthetic read checks completed. Windows native build, WebView2 demo, WeChat integration and authorized media coverage remain pending. See the [Windows acceptance checklist](phase0-audit.md#72-无法在-wsl-完成的验证status-blocked--需在-windows-原生环境执行). |
| CL-01 | Adapter and immutable snapshot merged in [PR #3](https://github.com/yuanbaoerer/ChatLens/pull/3) (`a3fd26e`). | 19 Analytics tests and 8 Moect baseline tests passed in WSL with Python 3.11 before merge. Import of an authorized Windows export remains pending. |
| CL-02 | Analyzer/service/run lifecycle merged in [PR #4](https://github.com/yuanbaoerer/ChatLens/pull/4) (`7185173`). | 47 Analytics tests and 8 Moect baseline tests passed in WSL with Python 3.11 before merge. Windows native workspace-lock, WebView2 demo and WeChat integration checks remain pending. |
| CL-03 | Next implementation task. | Planned scope is image/GIF/sticker detection with exact/near fixtures, missing-asset exclusion and caption/GIF safeguards. |

## Task roadmap

| ID | Priority | Task | Dependency | Acceptance |
|---|---|---|---|---|
| CL-00 | P0 | Audit and reproduce Moect baseline | — | actual file/line seam map, baseline build/demo evidence, authorized media coverage; missing capabilities documented |
| CL-01 | P0 | Adapter and immutable snapshot | 00 | stable IDs, paging/time range, media status, JSONL fixture |
| CL-02 | P0 | Analyzer/service/run lifecycle | 01 | typed registry, progress/cancel, local run persistence |
| CL-03 | P0 | Image/GIF/sticker detection | 02 | exact/near fixtures, missing assets excluded, caption and GIF safeguards |
| CL-04 | P0 | Text/link/forward detection | 02 | normalization, stable grouping, conservative URL rules |
| CL-05 | P0 | Generation provider | 02 | mock + opt-in real endpoint, schema validation and budgets |
| CL-06 | P0 | Objective/roast summary | 04,05 | chunk+reduce, grounded facts, injection tests |
| CL-07 | P0 | Embedding retrieval | 04 | bounded candidates, recall measurement, local baseline |
| CL-08 | P0 | Jev provider | 02 | official API verified, mock/real test, failure uncertainty |
| CL-09 | P0 | Semantic decision and clustering | 07,08 | update/conflict veto, nontransitive cluster tests |
| CL-10 | P0 | Activity and topics | 02,05 | exact stats, evidence-backed topic labels |
| CL-11 | P0 | GUI workflow | 01,02 | select group/range, quality, progress/cancel, consent |
| CL-12 | P0 | Report composer/export | 03,04,06,09,10,11 | evidence drill-down, denominators, partial/escaped redacted export |
| CL-13 | P0 | Evaluation | 02 onward | four comparison baselines, labeled holdout, published non-sensitive metrics |
| CL-14 | P0 | Privacy/security | 01 onward | opt-in, redaction, isolation, secret/log tests; applies to all phases |
| CL-15 | P0 | Windows packaging/E2E | all P0 | original features + complete desktop workflow, source/license compliance |
| CL-16 | P1 | Quotes and report styles | 12 | exact source quotes and style regeneration |
| CL-17 | P1 | Incremental analysis/cache | 12 | idempotent refresh and versioned invalidation |
| CL-18 | P1 | Periodic reports | 12 | user-selected historical periods, no background monitoring |

Sequence: CL-00 first alone, then adapter/lifecycle; deterministic report vertical slice and GUI shell; ordinary LLM; Jev/embedding; full report and evaluation. Privacy starts with first data handling. No broad infrastructure or unsupported live monitoring. A partial deterministic product is not the full MVP; release after complete P0 with honest coverage.

## Original CL-00 task brief
Read AGENTS.md and docs/chatlens. Work only on CL-00. On Windows reproduce upstream documented Python 3.11, npm frontend build and demo/installer where feasible. Trace actual read-only service/export/media/bridge/task seams with file+line pointers and data examples (synthetic only in repo); record version, baseline commit, command outputs, missing media and build limitations. Validate authorized sample locally without uploading or publishing it; if environment lacks Windows/WeChat, document exact user-run checks and mark blocked. Propose minimal adapter and GUI integration seams and tests. Add audit report docs/chatlens/phase0-audit.md, no functional change, no credentials, no security bypass, no automatic merge. Open PR to this fork; stop after CL-00.
