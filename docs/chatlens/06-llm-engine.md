# Generation and report narratives

GenerationProvider distinct from DecisionProvider. Start with configurable OpenAI-compatible endpoint/model/API key/timeout/concurrency/token budget, verify provider contract. Never hardcode credentials; use OS credential storage where available, env for dev, mask logs. Local model endpoint possible. LLM and Jev independently optional.

Input: ordered topic/dialogue chunks with local pseudonyms and evidence IDs, timestamps, reply context; include computed stats, representative content, duplicate counts and updates. Keep original data; logical dedup for summarization only. Group by time/context and token budget; overlap or quote context where needed; chunk summaries with evidence IDs then reduce across chunks. Avoid sampling bias, document coverage and truncation. Do not require embeddings for basic text summary; topic counts computed by code from explicit assignment, not guessed by LLM.

Outputs: objective summary, roast summary, topic labels/observations; P1 quotes, weekly/monthly style. Structured schema for narrative sections and evidence refs; validate IDs against snapshot, quotations verbatim, all numerical statements grounded in computed metrics. Reject unsupported claims or mark unverifiable. Humor targets group behavior, not protected traits, private sensitive matters, harassment or personality inference. No fabricated quotes or numbers. Treat all chat content as untrusted quoted data, no tool calls/links execution. Redact before cloud calls, no full raw history upload by default.

Prompts versioned under analytics/prompts; separate facts and style. Example roast: '同一篇文章被转发了十次，群里的广播系统似乎没有去重。' Only use ten if metric supports it. User can regenerate style using existing structured evidence, no rerun of all detectors. Evaluate factual consistency, coverage, evidence accuracy, humor usefulness, cost/latency and injection resistance.

## CL-05 generation provider implementation

`analytics/providers/generation.py` provides the typed `GenerationProvider` boundary; CL-06 owns prompts, context construction, redaction and analyzer integration.

- `GenerationRequest` carries ordered system/user/assistant messages, an optional JSON schema, output-token limit, temperature and metadata. `GenerationResponse` carries only parsed structured output, provider/model ids, token usage, finish reason and safe diagnostics; raw prompt/response text is not logged or persisted by the provider.
- `MockGenerationProvider` is deterministic and network-free for CI/local mode. It validates its output against the request schema and can share a `GenerationBudget`.
- `OpenAICompatibleGenerationProvider` is disabled by default and requires explicit `enabled=True`. Endpoint, model, timeout, concurrency, request-size cap, response-size cap and optional API-key environment are configurable. Local HTTP endpoints are supported; remote endpoints must use HTTPS. Redirects are refused so bearer credentials cannot be forwarded to another host. Credentials are never hardcoded, logged or included in `repr`.
- Schema validation uses a small dependency-free JSON Schema subset (`type`, `required`, `properties`, `additionalProperties`, `items`, length/item/number bounds, `enum`, `pattern`). Unsupported keywords fail closed. Errors omit model and chat values.
- `GenerationBudget` enforces call/input/output/total-token limits with in-flight reservations and post-call actual usage; concurrent calls cannot all pass against the same balance. Token estimates use UTF-8 byte length to avoid CJK underestimation; partial or missing provider usage is estimated conservatively.
- Request cancellation is checked before/after provider work; responses with HTTP errors, truncated HTTP bodies, malformed envelopes, invalid JSON, schema violations, oversized request/response bodies, timeouts or network failures raise typed errors without echoing response content.
- The provider boundary does not fetch links, execute tools, redact messages or call models unless the caller has constructed an enabled request. CL-11/CL-14 remain responsible for GUI consent, OS credential storage, redaction preview and deletion controls.
