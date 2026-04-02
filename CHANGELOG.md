# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[unreleased]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.4.1...v0.5.0
[0.4.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scttfrdmn/quick-suite-router/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/scttfrdmn/quick-suite-router/releases/tag/v0.1.0
