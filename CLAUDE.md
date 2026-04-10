# CLAUDE.md — Project Context for Claude Code

## Project Overview

This is **quick-suite-router** — a CDK-deployable reference
architecture that extends Amazon Quick Suite with multi-provider LLM
access through Bedrock AgentCore Gateway.

The core idea: universities have existing AI subscriptions (OpenAI site
licenses, Google Gemini via Workspace, Anthropic agreements). Instead of
asking them to abandon those investments for Quick Suite's built-in LLM,
this router lets them bring their existing subscriptions INTO Quick Suite
with full AWS governance (Bedrock Guardrails, CloudTrail, CloudWatch
metering) applied to every call regardless of provider. The existing AI
subscription becomes the on-ramp to AWS, not the competitor.

## Project Tracking

Work is tracked in GitHub — not in local files. Do not add TODO lists or task
tracking to CLAUDE.md or create TODO.md files.

- **Issues:** https://github.com/scttfrdmn/quick-suite-router/issues
- **Milestones:** https://github.com/scttfrdmn/quick-suite-router/milestones
- **Project board:** https://github.com/users/scttfrdmn/projects/44
- **Changelog:** CHANGELOG.md (keepachangelog format, semver 2.0)

To report a bug or propose a feature, open a GitHub Issue with the appropriate
label. All release planning happens via milestones.

## Current State — v0.12.0

### File Inventory (35 files)

**Code:**
- `app.py` — CDK entry point
- `cdk.json` — CDK config with context flags (enable_cache, cache_ttl_minutes, budget_caps_secret_arn, enable_vpc, vpc_id, cors_allowed_origin, rate_limit_per_minute, rate_limit_per_day, enable_content_logging)
- `requirements.txt` — CDK dependencies
- `requirements-dev.txt` — dev/test dependencies
- `stacks/__init__.py` — empty
- `stacks/model_router_stack.py` — main CDK stack (Cognito, Secrets Manager,
  Lambdas, API Gateway, Bedrock Guardrail, DynamoDB cache + spend ledger, CloudWatch dashboard,
  optional VPC with private endpoints, per-user rate limiting usage plan, content audit logging)
- `stacks/multi_region_stack.py` — multi-region deployment support
- `lambdas/router/handler.py` — task classification, provider selection
  (capability + context window matching), cache check, fallback logic, spend
  record write, budget cap enforcement, PHI routing filter, dry-run mode,
  Cognito claims extraction, CORS origin control, content audit logging
  (SHA-256 hashes)
- `lambdas/common/python/provider_interface.py` — shared governance utilities
  (guardrails, CloudWatch metrics, DynamoDB cache helpers, spend ledger write,
  cost_usd computation, spend department query, token estimation)
- `lambdas/authorizer/handler.py` — Lambda authorizer for per-user rate
  limiting; decodes Cognito JWT, extracts `sub`, returns IAM Allow policy
  with `usageIdentifierKey = sub`
- `lambdas/query-spend/handler.py` — AgentCore Lambda target; queries qs-router-spend
  and aggregates cost by department/user/tool/date; enforces Cognito claims-based authorization
- `lambdas/providers/bedrock_provider.py` — Bedrock Converse + converse_stream API
- `lambdas/providers/anthropic_provider.py` — Anthropic Messages API (blocking + streaming)
- `lambdas/providers/openai_provider.py` — OpenAI Chat Completions API (blocking + streaming)
- `lambdas/providers/gemini_provider.py` — Google Generative AI API (blocking + streaming)
- `lambdas/guardrail-version-updater/handler.py` — internal Lambda; updates SSM param for guardrail version without redeploy
- `lambdas/key-rotation-checker/handler.py` — internal Lambda; weekly check that provider API key secrets have been rotated within `KEY_ROTATION_MAX_AGE_DAYS`; emits `KeyRotationOverdue` CloudWatch metric

**Config:**
- `config/routing_config.example.yaml` — provider preference lists per tool
- `config/routing_config.yaml` — active routing config with `model_capabilities` + `model_context_windows` registries
- `quicksuite/openapi_spec.json` — OpenAPI spec for Quick Suite import
- `quicksuite/agent-template.json` — AgentCore agent template

**Docs:**
- `docs/architecture.md` — full architecture overview with ASCII diagrams
- `docs/setup-bedrock.md` — Bedrock setup (IAM, model access)
- `docs/setup-anthropic.md` — Anthropic direct API setup
- `docs/setup-openai.md` — OpenAI setup with site license instructions
- `docs/setup-gemini.md` — Gemini setup with Google Workspace instructions
- `docs/quicksuite-integration.md` — AgentCore Gateway + Quick Suite MCP setup
- `docs/compliance.md` — HIPAA-ready deployment guide (VPC isolation, PHI tagging, CloudTrail, Guardrail hardening)

**GTM (go-to-market for BD/AM peers):**
- `gtm/bd-playbook.md` — discovery questions, demo script, competitive positioning
- `gtm/am-talking-points.md` — stakeholder messaging, value chain
- `gtm/objection-handling.md` — 10 objections with full responses

**Scaffolding:**
- `.gitignore`
- `LICENSE` (Apache 2.0)
- `README.md`

## Architecture

```
Quick Suite
    | MCP Actions Integration
    v
AgentCore Gateway (MCP server)
    | HTTPS
    v
API Gateway + Cognito (OAuth 2.0 client_credentials)
    |
    +-- Lambda Authorizer (per-user rate limiting via JWT sub)
    |
    v
Router Lambda
    |-- DynamoDB cache (optional, TTL-based)
    |-- Task classification (analyze/generate/research/summarize/code/extract)
    |-- Capability + context window matching
    |-- PHI routing filter (restricts to Bedrock-only)
    |-- Provider selection (config-driven preference list)
    |-- Dry-run mode (cost estimate without invocation)
    +-- Fallback on error
         |
    +----+--------+----------+----------+
    v             v          v          v
Bedrock       Anthropic   OpenAI    Gemini
(Converse     (Messages   (Chat     (GenerativeAI
 API, IAM)     API)       Compl.)    API)
    |             |          |          |
    +-------------+----+-----+----------+
                       v
              Bedrock Guardrails (input + output)
              CloudWatch Metrics (per-provider tokens, latency, guardrails)
              Spend Ledger (DynamoDB, per-department budget caps)
              Content Audit Log (optional SHA-256 hashes)
              CloudTrail (automatic via API Gateway)
```

### Tools

Six tool endpoints exposed through API Gateway:

1. **analyze** — analytical reasoning over structured data
2. **generate** — creative text generation (supports SSE streaming)
3. **research** — deep research with optional `grounding_mode: "strict"` for citation tracking
4. **summarize** — condensation and summarization
5. **code** — code generation, review, and explanation
6. **extract** — structured extraction from text (`effect_sizes`, `confounds`, `methods_profile`, `open_problems`, `citations`)

Plus one AgentCore Lambda target:
- **query_spend** — query spend ledger by department/user/tool/date

## Key Design Decisions

1. **Task-oriented tools, not provider-oriented.** Quick Suite users see
   `analyze`, `generate`, `research`, `summarize`, `code`, `extract` — not
   "call Claude" or "call GPT". The router picks the best available provider.

2. **Config-driven routing.** `routing_config.yaml` has a preference list
   per tool. The router walks the list, skipping providers without
   credentials. Customers can force a provider per-request via the
   `provider` field.

3. **Automatic fallback.** If the preferred provider errors (rate limit,
   timeout, etc.), the router tries the next one in the preference list.
   Fallback metadata is included in the response.

4. **Governance on ALL providers.** Even direct-to-OpenAI and direct-to-
   Gemini calls get Bedrock Guardrails applied (input and output) and
   CloudWatch metering. This is the value proposition.

5. **No pip dependencies in Lambdas.** All provider Lambdas use only
   `boto3` (always available in Lambda) and `urllib` (stdlib). No
   vendor SDKs, no layers to manage, no version conflicts.

6. **Cache only low-temperature requests.** The DynamoDB cache only
   activates when temperature <= 0.3 to avoid caching nondeterministic
   responses. Callers can also set `skip_cache: true`.

7. **Secrets Manager for credentials, not env vars.** Provider Lambdas
   receive the secret ARN, not the key. Keys are fetched at runtime and
   cached in Lambda memory for the execution lifetime.

8. **Capability + context window routing (v0.11.0).** `select_provider()`
   accepts `required_capabilities` and `context_budget`; skips providers
   missing any required capability or with insufficient context window.
   Token estimation via `estimate_tokens()` heuristic. Returns specific
   error codes (`context_limit_exceeded`, `unsatisfiable_capabilities`)
   when no provider qualifies.

9. **PHI never leaves AWS.** `data_classification: "phi"` silently
   restricts the provider candidate set to Bedrock only. Non-Bedrock
   providers never receive PHI. Returns 503 if Bedrock is unavailable.

10. **Dry-run before spend.** `dry_run: true` returns cost estimate,
    selected provider/model, and token estimate without invoking any
    model or writing to the spend ledger.

## v0.11.0 Capability Routing

- Model capability registry: `model_capabilities` + `model_context_windows`
  top-level dicts in `routing_config.yaml`; keyed by `provider/model_id`;
  non-breaking alongside existing `preferred` lists
- `select_provider()` returns 3-tuple `(provider_key, model_id, skip_reason)`;
  skips providers missing required capabilities or with insufficient context
- `estimate_tokens(text)` heuristic: `max(1, len(text) // 4)` applied to
  prompt + system + context + max_tokens
- `tokens_in_estimate` included in every successful response
- Fallback chain respects the same capability + context filters

## v0.12.0 Operational Controls

**Dry-run mode:**
- `dry_run: true` in any request body returns `{estimated_cost_usd,
  selected_provider, selected_model, tokens_in_estimate}` without invoking
  any model or writing spend ledger
- Capability + context filters still apply

**Per-user rate limiting:**
- `lambdas/authorizer/handler.py` Lambda authorizer decodes Cognito JWT,
  extracts `sub`, returns IAM Allow policy with `usageIdentifierKey = sub`
- CDK creates per-user usage plan (`PerUserRateLimitPlan`) when
  `rate_limit_per_minute` context is set
- `throttle.rate_limit = rpm/60`, `burst_limit = rpm*2`
- `quota.limit` from `rate_limit_per_day` (default 1000)

**Extract tool (6th endpoint):**
- Structured extraction from text via `extraction_types` list
  (e.g. `effect_sizes`, `confounds`, `methods_profile`, `open_problems`,
  `citations`)
- Auto-requires `structured_output` capability for provider selection
- Providers enable JSON mode (OpenAI: `response_format={"type":"json_object"}`,
  Gemini: `responseMimeType`)
- `open_problems` type extracts `[{gap_statement, domain, confidence}]`;
  optional `store_at_uri: "s3://..."` persists gap list for clAWS watch use

**Grounding mode on research tool:**
- `grounding_mode: "strict"` injects citation directive
- Response gains `sources_used`, `grounding_coverage`,
  `low_confidence_claims` parsed from trailing JSON block

## What's Built

- Provider routing + fallback (config-driven preference lists per tool)
- `apply_guardrail_safe()` — fail-closed Bedrock Guardrails wrapper on all external provider calls; emits `GuardrailError` CW metric on failure
- `GuardrailApplied` CloudWatch metric; Guardrail Coverage dashboard widget
- Multi-turn conversation history: JSON list in `context` field prepended as native messages (Anthropic/OpenAI) or `contents` array with role mapping (Gemini)
- Department overrides: per-department provider preference lists in routing config
- DynamoDB response cache (configurable TTL, temperature <= 0.3 only)
- Cognito OAuth client_credentials for AgentCore Gateway
- CloudWatch dashboard with per-provider token/latency/guardrail metrics
- SSE streaming for `generate` and `research` tools (`stream: true` flag); buffered-streaming pattern returns `chunks` list + assembled `content`; all four providers supported; guardrails applied to assembled text
- Spend ledger DynamoDB table + `query_spend` AgentCore Lambda target; per-department budget cap enforcement (HTTP 402 on breach); `compute_cost_usd()` price table
- VPC isolation: `enable_vpc` CDK context flag places all Lambdas in a private VPC with no internet egress; Gateway endpoints for S3/DynamoDB; Interface endpoints for Secrets Manager, Lambda, CloudWatch, X-Ray, Bedrock
- PHI routing: `data_classification: "phi"` restricts provider set to Bedrock only
- CORS origin control: `CORS_ALLOWED_ORIGIN` env var (CDK context `cors_allowed_origin`)
- Spend ledger authorization: `department`/`user_id` extracted from Cognito JWT claims; body fallback for direct invocation
- `query-spend` Lambda: non-admin callers restricted to own department/user by Cognito groups (`finance_admin`, `admin`)
- Content audit logging: SHA-256 hashes of prompt + response when `enable_content_logging=true` CDK context flag is set
- Guardrail version via SSM: all provider Lambdas read `/quick-suite/router/guardrail-version` at cold start; `guardrail-version-updater` Lambda updates SSM param without `cdk deploy`
- v0.11.0 capability + context window routing with model registry, token estimation, specific error codes
- v0.12.0 dry-run mode, per-user rate limiting (Lambda authorizer + API Gateway usage plans)
- v0.12.0 `extract` tool with structured extraction types + `open_problems` S3 persistence
- v0.12.0 `grounding_mode: "strict"` on research tool with citation tracking
- `docs/compliance.md`: HIPAA-ready deployment guide (VPC walkthrough, PHI tagging, CloudTrail, Guardrail hardening for healthcare)
- 263 unit tests; cfn-lint + CDK synth in PR-blocking CI job

## Strategic Context

This is an AWS BD play for higher education accounts. The owner (Scott
Friedman) works in AWS business development focused on academic research
computing. The target customers are R1 universities.

The wedge strategy: "We already have an OpenAI subscription" is the most
common objection to Quick Suite adoption. This router turns that objection
into an on-ramp. The university keeps their OpenAI spend, gains governance,
and runs orchestration on AWS. Once orchestration is on AWS, migrating
individual model calls to Bedrock is a config change.

The GTM materials in `gtm/` are for Scott's BD peers and account managers.
They contain discovery questions, demo scripts, objection handling, and
competitive positioning. These are internal AWS audience materials.

## Code Style

- Python 3.12, no type annotations beyond what's in provider_interface.py
- Provider Lambdas are deliberately thin (~80 lines) — normalize the
  vendor API, that's it
- CDK stack uses L2 constructs where available, L1 (Cfn) for Guardrails
- Error responses use the same schema as success responses (content="",
  error="message") so the router can handle them uniformly
- Logging at INFO level with structured JSON for CloudWatch Logs Insights
