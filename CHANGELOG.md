# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.12.0] - 2026-04-07

### Added
- **Issue #37: Dry-run mode** — Callers may set `"dry_run": true` in any request body to get a cost estimate without invoking any model or writing to the spend ledger. The response includes `dry_run: true`, `estimated_cost_usd` (float), `selected_provider`, `selected_model`, and `tokens_in_estimate`. All capability and context-window filters still apply — if no provider satisfies the request, the same 400 error is returned as in live mode. `compute_cost_usd()` from `provider_interface.py` is reused for the estimate; no new pricing logic.
- **Issue #36: Per-user rate limiting** — New `lambdas/authorizer/handler.py` Lambda authorizer decodes the Cognito JWT payload, extracts the `sub` claim, and returns an IAM Allow policy with `usageIdentifierKey = sub`. API Gateway uses this key to enforce per-user throttle and quota against the per-user usage plan. CDK: when the `rate_limit_per_minute` context variable is set, a second usage plan (`PerUserRateLimitPlan`) is created alongside the existing global key-based plan, with `throttle.rate_limit = rpm / 60` (requests per second), `burst_limit = rpm * 2`, and a daily `quota.limit` from `rate_limit_per_day` context (default 1000). The authorizer Lambda is also deployed (CfnOutput: `CognitoJwtAuthorizerArn`); wire it into API Gateway as a custom TOKEN authorizer to activate per-user keying.

- **Issue #38: `extract` tool** — Sixth tool endpoint for structured extraction from text documents. Callers pass `extraction_types` (required list of strings: `effect_sizes`, `confounds`, `methods_profile`, `open_problems`, `citations`, etc.); the router auto-requires `structured_output` capability so only capable providers are selected. Provider Lambdas inject an extraction directive into the system prompt; OpenAI uses `response_format={"type":"json_object"}`, Gemini uses `responseMimeType: "application/json"`. Response includes `extracted_fields` dict parsed from provider JSON output. Optional `store_at_uri: "s3://..."` writes the `open_problems` list to S3 after extraction.
- **Issue #39: `open_problems` extraction type** — When `extraction_types` includes `"open_problems"`, providers additionally instruct the model to return a list of `{gap_statement, domain, confidence}` objects under that key. Callers may set `store_at_uri` to persist the gap list to S3 for use by clAWS cross-discipline signal watches.
- **Issue #40: `grounding_mode: "strict"` on `research` tool** — Callers may set `grounding_mode: "strict"` to activate citation-grounded research mode. All four provider Lambdas inject a grounding directive into the system prompt requiring inline citation, `[LOW CONFIDENCE]` prefixes for unsupported claims, and a trailing JSON block with `sources_used`, `grounding_coverage` (0.0–1.0), and `low_confidence_claims`. These fields are parsed from the response and returned as top-level response keys. Default behaviour (`grounding_mode: "default"` or omitted) is unchanged.

### Tests
- 8 new tests: `TestDryRunMode` (5) — model invoke not called in dry_run, correct fields returned, capability filter applies, no spend ledger write, omitted flag executes normally; `TestPerUserRateLimitingAuthorizer` (3) — `usageIdentifierKey` equals decoded sub claim, missing token raises Unauthorized, malformed JWT raises Unauthorized.
- 8 new tests: `TestExtractTool` (5) — happy path returns `extracted_fields`, missing `extraction_types` → 400, provider without `structured_output` capability → 400 `unsatisfiable_capabilities`, `store_at_uri` writes S3 on `open_problems`, open_problems list has expected keys; `TestGroundingModeStrict` (3) — strict mode response includes `sources_used`/`grounding_coverage`/`low_confidence_claims`, default mode omits those fields, invalid grounding_mode treated as default.

## [0.11.0] - 2026-04-07

### Added
- **Issue #34: Model capability routing** — `routing_config.yaml` gains two new top-level dicts: `model_capabilities` (keyed by `provider/model_id`, value is a list of capability strings such as `vision`, `long_context`, `function_calling`, `structured_output`) and `model_context_windows` (keyed by `provider/model_id`, value is the token limit integer). Callers may pass a `capabilities` field (string or list) in the request body; any provider whose capability set does not satisfy all required capabilities is skipped. The existing `preferred` list format is unchanged — backward compatible.
- **Issue #35: Context window enforcement** — `estimate_tokens(text)` helper (len // 4 heuristic) computes a total context budget from prompt + system prompt + conversation context + `max_tokens`. Each candidate provider is skipped if its `model_context_windows` entry (when configured) is smaller than the budget. A window of `0` (unconfigured) is never treated as a constraint.
- `select_provider()` now returns a 3-tuple `(provider_key, model_id, skip_reason)` where `skip_reason` is `""` on success, `"context_limit_exceeded"`, or `"unsatisfiable_capabilities"`. The fallback chain respects the same capability and context filters.
- `handle_tool_invocation()` maps skip reasons to distinct HTTP error responses: `context_limit_exceeded` → HTTP 400 with `code` and `tokens_in_estimate` fields; `unsatisfiable_capabilities` → HTTP 400 with `code` and the requested capability list. Every successful response now includes `tokens_in_estimate`.
- `_get_model_caps()` and `_get_context_window()` helpers read from the new config dicts (default to `[]` / `0` when absent).

### Tests
- 12 new tests in `TestCapabilityAndContextRouting`: capability match routes correctly, missing cap skips provider, all caps missing → 400 `unsatisfiable_capabilities`, context fits model → selected, context exceeds smaller model → falls to larger, context exceeds all → 400 `context_limit_exceeded`, `tokens_in_estimate` in 400 body, `tokens_in_estimate` in 200 body, `capabilities` as string coerced to list, unconfigured context window never causes skip, fallback chain respects capability filter.

## [0.10.0] - 2026-04-06

### Security
- Fixed full request body logged to CloudWatch: `logger.info(json.dumps(event))` in router and query-spend handlers dumped the entire Lambda event (including `prompt`, `context`, `user_id`) at INFO level; replaced with a safe structured subset (`tool`, `path`, `httpMethod`, `requestId`) that never includes request body fields (closes #44)
- Fixed Bedrock IAM policy allowing InvokeModel in any region: `arn:aws:bedrock:*::foundation-model/*` wildcard was replaced with `arn:aws:bedrock:{region}::foundation-model/*` scoped to the stack's deployment region (closes #45)
- Fixed prompt injection via unvalidated chat history in `context` field: `_parse_context()` in all three non-Bedrock providers (OpenAI, Anthropic, Gemini) now validates role values against `{"user", "assistant", "system"}`, enforces `str` content type, caps per-message content at 4,000 chars, and truncates history arrays exceeding 50 messages; any malformed entry rejects the entire history (closes #46)
- Fixed budget caps loaded once at cold start with fail-open on error: `_budget_caps_loaded` is now reset to `False` on Secrets Manager failure so the next invocation retries; new `BUDGET_CAPS_REQUIRED` env var / `budget_caps_required` CDK context flag enables fail-closed mode (raises, causing Lambda 500) for environments where budget enforcement is mandatory (closes #47)
- Fixed spend ledger DynamoDB table missing deletion protection and PITR: `qs-router-spend` table now has `point_in_time_recovery=True` and `deletion_protection=True` (closes #48)
- Added key rotation enforcement: new `key-rotation-checker` Lambda runs weekly via EventBridge Scheduler, checks each provider API key secret's `LastChangedDate` against `KEY_ROTATION_MAX_AGE_DAYS` (default 90, CDK context var), and emits a `KeyRotationOverdue` CloudWatch metric + ERROR log for overdue secrets (closes #49)
- Fixed missing Content-Type validation: `handle_tool_invocation()` now checks the `Content-Type` / `content-type` header and returns HTTP 415 if set to a non-JSON MIME type; missing header is allowed for backward compatibility with direct Lambda invocations (closes #50)

### Added
- `lambdas/key-rotation-checker/handler.py`: lightweight internal Lambda (~70 lines) that audits API key secret ages and emits CloudWatch metrics
- `tests/test_security_hardening.py`: 33 new tests covering all seven security fixes (safe logging, context validation, budget caps retry, content-type rejection, key rotation checker, CDK Bedrock IAM scoping, PITR/deletion protection)

## [0.9.0] - 2026-04-06

### Security
- Fixed CORS wildcard: `Access-Control-Allow-Origin: *` was hardcoded on all responses; replaced with `CORS_ALLOWED_ORIGIN` env var (default `*` with CDK warning); set `cors_allowed_origin` in CDK context to restrict to your Quick Suite domain (closes #43)
- Fixed spend ledger authorization bypass: `department` and `user_id` were taken from the request body, allowing any authenticated caller to attribute spend to another department; now extracted from Cognito JWT claims (`sub`, `custom:department`) injected by API Gateway; falls back to body values for direct Lambda invocation (testing/development) (closes #41)
- Fixed `query-spend` Lambda exposing org-wide cost data: any caller could query any department's spend without restriction; non-admin callers (no `finance_admin`/`admin` in `cognito:groups`) are now restricted to their own department and user_id; admin callers are unrestricted; no-claims path (direct Lambda invocation) is backward-compatible (closes #42)

### Added
- Content audit logging (`#33`): when `enable_content_logging=true` CDK context flag is set, router Lambda emits a structured JSON log record after every successful provider call with fields `audit_log: "content"`, `timestamp`, `request_id`, `tool`, `provider`, `model`, `tokens_in`, `tokens_out`, `cost_usd`, `department`, `prompt_hash` (SHA-256), `response_hash` (SHA-256); raw text is never logged; CDK creates dedicated `/quick-suite/router/content-audit` log group with 1-year retention (closes #33)
- Guardrail version management via SSM (`#32`): all four provider Lambdas now read the active guardrail version from SSM parameter `/quick-suite/router/guardrail-version` at cold start via `_load_guardrail_version()`; falls back to `GUARDRAIL_VERSION` env var on SSM error; new `guardrail-version-updater` Lambda accepts `{"version": "N"}` and updates the SSM parameter without requiring a `cdk deploy`; CDK grants SSM read to all provider Lambdas and SSM write to the updater Lambda (closes #32)

### Changed
- `_load_handler()` in tests now supports `extra_env` dict for env var overrides (used by CORS and content logging tests)

## [0.8.0] - 2026-04-02

### Added
- VPC Lambda deployment (`enable_vpc` CDK context flag, default false): when enabled, all Lambda functions run in a private isolated VPC with no internet egress; Gateway VPC endpoints for S3 and DynamoDB; Interface VPC endpoints for Secrets Manager, Lambda invocation, CloudWatch, X-Ray, Bedrock, and Bedrock Runtime; optional `vpc_id` context var to reuse an existing VPC; `AWSLambdaVPCAccessExecutionRole` added to Lambda IAM roles when VPC mode is active; `VpcId` CloudFormation output (closes #8)
- PHI-tagged request routing: `data_classification: "phi"` field on any tool call silently restricts the provider candidate set to Bedrock only; non-Bedrock providers (Anthropic, OpenAI, Gemini) excluded regardless of preference lists or explicit overrides; if no Bedrock provider is available, standard 503 is returned (no PHI reaches external providers); PHI filtering applied in both primary selection and fallback chain (closes #9)
- `_NON_BEDROCK_PROVIDERS` constant in `handler.py` enumerates external providers for PHI exclusion
- `docs/compliance.md`: HIPAA-ready deployment guide covering `enable_vpc` walkthrough, PHI tagging usage, CloudTrail setup recommendations, Bedrock Guardrail hardening for healthcare, and external provider opt-out note; target audience is health science school IT administrators (closes #10)
- 9 new unit tests for PHI routing: Bedrock-only selection, non-Bedrock exclusion, no-Bedrock-available 503, case-insensitive PHI field, explicit non-Bedrock override ignored, non-PHI requests unaffected, end-to-end invocation test

## [0.7.0] - 2026-04-02

### Added
- Spend ledger DynamoDB table `qs-router-spend` (PK: `department#user_id`, SK: `tool#date#timestamp`); TTL attribute `expires_at` set to 13 months (closes #5)
- `spend_record_write()` in `provider_interface.py`: writes cost_usd, token counts, provider, model, department, user_id, tool, date, timestamp after every successful tool call; `cost_usd` computed from per-model price table in `compute_cost_usd()`; fails silently (never breaks the caller)
- `compute_cost_usd()` in `provider_interface.py`: hardcoded price table for Bedrock (Claude Sonnet, Nova Pro/Lite), Anthropic direct, OpenAI (GPT-4o, GPT-4o-mini), Gemini (2.5 Pro/Flash); falls back to defaults for unknown models
- `query_spend` AgentCore Lambda target (`lambdas/query-spend/handler.py`): inputs `department`, `user_id`, `date_range`, `group_by`; returns aggregated `cost_usd`, `token_count_in`, `token_count_out`, `call_count` grouped by dimension; returns empty results (not error) for unknown department/user (closes #6)
- Budget cap enforcement: CDK context var `budget_caps_secret_arn` → Secret JSON `{department: monthly_usd_cap}`; loaded once at Lambda startup; router checks current-month department spend before invoking provider; returns `{"error": "budget_exceeded", ...}` with HTTP 402 when cap exceeded; fail-open on Secrets Manager or DynamoDB error (closes #7)
- `department` defaults to `"default"`, `user_id` defaults to `"anonymous"` in spend records when not provided by caller
- CDK: `SpendLedger` DynamoDB table, `QuerySpendFunction` Lambda, IAM grants; `SpendTableName` and `QuerySpendFunctionName` CloudFormation outputs
- 34 new unit tests covering cost computation, spend ledger writes, query_spend aggregation/grouping, budget cap enforcement (blocked at cap, fail-open), and router integration

## [0.6.0] - 2026-04-02

### Added
- SSE streaming support for `generate` and `research` tool endpoints (`stream: true` request flag)
- Buffered streaming pattern: providers collect SSE chunks from upstream LLM APIs and return them in a `chunks` list alongside the fully assembled `content` field; `streaming: true` added to response when active
- Anthropic streaming: `_invoke_streaming()` in `anthropic_provider.py` handles `message_start`, `content_block_delta`, `message_delta`, and `message_stop` SSE event types; token counts sourced from `message_delta` usage
- OpenAI streaming: `_invoke_streaming()` in `openai_provider.py` parses `data:` SSE lines, handles `[DONE]` terminator; token counts sourced from `stream_options.include_usage` chunk
- Bedrock streaming: `_invoke_streaming()` in `bedrock_provider.py` uses `converse_stream()`, maps `contentBlockDelta` events to chunks; `metadata` event provides token counts
- Gemini streaming: `_invoke_streaming()` in `gemini_provider.py` uses `streamGenerateContent?alt=sse` endpoint; `usageMetadata` in final chunk provides token counts
- Guardrails applied to fully assembled text in all streaming paths (not individual chunks)
- Stream flag silently ignored (with structured log warning) for non-streaming tools (`analyze`, `summarize`, `code`)
- Full cache write for streaming responses at low temperature (same as non-streaming)
- 31 new unit tests covering all streaming paths and edge cases

## [0.5.0] - 2026-04-02

### Added
- Multi-turn conversation history: `context` field accepts a JSON list of prior messages; prepended as native messages for Anthropic/OpenAI, mapped to `contents` array with role mapping for Gemini
- `GuardrailApplied` CloudWatch metric emitted on every `apply_guardrail` call; Guardrail Coverage widget added to CloudWatch dashboard

### Fixed
- Cache key now includes tool name, preventing cross-tool cache collisions when requests share identical prompts across different tool endpoints
- `_preferred_for()` logs a structured JSON warning when a `department` value is not found in `department_overrides`; falls back to global routing
- Input validation boundary tests: temperature 0–1, `max_tokens` 1–16384, prompt size ≤ 100,000 bytes

## [0.4.1] - 2026-04-02

### Fixed
- CI `test` job: add `setup-node@v4` and `npm install -g aws-cdk` so `cdk synth` succeeds (CDK CLI requires Node.js)
- `stacks/model_router_stack.py`: fix ruff I001 import-sort order
- `stacks/multi_region_stack.py`: remove unused `hosted_zone` variable (F841)

## [0.4.0] - 2026-04-01

### Added
- `department` dimension in `emit_usage_metrics()` (`provider_interface.py`): all CloudWatch token/latency/guardrail metrics now carry a `Department` dimension for per-department usage reporting
- API Gateway usage plan and API key (`ModelRouterUsagePlan`, `ModelRouterApiKey`) with configurable throttle rate/burst via `api_throttle_rate`/`api_throttle_burst` CDK context variables; `ApiKeyId` CloudFormation output added
- Multi-region failover preparation: `stacks/multi_region_stack.py` with Route 53 health-check and PRIMARY/SECONDARY failover `CfnRecordSet` pair; deployed automatically when `secondary_region` CDK context variable is set
- `quicksuite/agent-template.json` refreshed: placeholder names aligned with CDK output keys, `department` field added to all five tool input schemas, example payloads added, `department_overrides_example` section added

### Changed
- Router `handler.py` passes `department` keyword argument to all `emit_usage_metrics()` calls (cache hit, success, and fallback paths)
- `app.py` updated to support optional secondary-region multi-region stack deployment

## [0.3.0] - 2026-04-01

### Added
- Per-department routing overrides: `department` field in request body selects an alternate provider preference list from the new `department_overrides` section in `routing_config.yaml`; example overrides for `openai-only` and `bedrock-only` departments included in `config/routing_config.example.yaml`
- CloudWatch alarms: LatencyAlarm (p99 latency > 5 s), ErrorRateAlarm (error rate > 5%), FallbackRateAlarm (fallback rate > 20%); optional SNS email topic via `alarm_email` CDK context variable
- `FallbackInvoked` and `AllProvidersFailed` CloudWatch metrics emitted from router Lambda for alarm and dashboard visibility
- Integration test suite (`tests/test_integration_bedrock.py`) covering Bedrock provider success, guardrail-blocked responses, error handling, and full router→Bedrock chain using Substrate
- cfn-lint step in CI workflow validates synthesised CloudFormation template on every push and pull request

## [0.2.0] - 2026-04-01

### Added
- X-Ray active tracing on all five Lambda functions (router + four providers) — service map and latency percentiles with zero code changes
- Quick Suite MCP Actions Integration configuration template (`quicksuite/agent-template.json`) with placeholders for all post-deploy values
- Post-deploy helper script (`scripts/post-deploy.sh`) — retrieves CloudFormation outputs and prints provider secret population commands and AgentCore Gateway registration steps

### Changed
- README: added Known Limitations section covering streaming, guardrail coverage differences between Bedrock and external providers, cache scope, input/output size limits, provider availability detection window, and single-region constraint

## [0.1.0] - 2026-04-01

### Added
- Multi-provider LLM routing: Bedrock (Converse API), Anthropic Messages API, OpenAI Chat Completions, Google Gemini Generative AI
- Task classification into five tool types: analyze, generate, research, summarize, code
- Bedrock Guardrails applied to all provider calls — input and output filtering regardless of which LLM handles the request
- Automatic fallback chain: on provider error or rate-limit, router tries the next configured provider
- DynamoDB response cache for low-temperature (≤ 0.3) requests with configurable TTL
- Cognito OAuth 2.0 client credentials authentication for AgentCore Gateway integration
- CloudWatch usage metrics: token counts and latency per provider and tool
- Config-driven routing via `routing_config.yaml` — provider preferences per tool type
- Secrets Manager integration for external provider API keys (no env-var key storage)
- CDK stack with full infrastructure-as-code deployment (Cognito, API Gateway, Lambdas, DynamoDB, Guardrail, CloudWatch dashboard)

[unreleased]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.4.1...v0.5.0
[0.4.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/quick-suite-router/releases/tag/v0.1.0
