# CLAUDE.md — Project Context for Claude Code

## Project Overview

This is **quicksuite-model-router** — a CDK-deployable reference
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

- **Issues:** https://github.com/scttfrdmn/quick-suite-model-router/issues
- **Milestones:** https://github.com/scttfrdmn/quick-suite-model-router/milestones
- **Changelog:** CHANGELOG.md (keepachangelog format, semver 2.0)

To report a bug or propose a feature, open a GitHub Issue with the appropriate
label. All release planning happens via milestones.

## Current State

### File Inventory (25 files)

**Code:**
- `app.py` — CDK entry point
- `cdk.json` — CDK config with context flags (enable_cache, cache_ttl_minutes)
- `requirements.txt` — CDK dependencies
- `stacks/__init__.py` — empty
- `stacks/model_router_stack.py` — main CDK stack (Cognito, Secrets Manager,
  Lambdas, API Gateway, Bedrock Guardrail, DynamoDB cache, CloudWatch dashboard)
- `lambdas/router/handler.py` — task classification, provider selection,
  cache check, fallback logic
- `lambdas/common/python/provider_interface.py` — shared governance utilities
  (guardrails, CloudWatch metrics, DynamoDB cache helpers)
- `lambdas/providers/bedrock_provider.py` — Bedrock Converse API
- `lambdas/providers/anthropic_provider.py` — Anthropic Messages API
- `lambdas/providers/openai_provider.py` — OpenAI Chat Completions API
- `lambdas/providers/gemini_provider.py` — Google Generative AI API

**Config:**
- `config/routing_config.example.yaml` — provider preference lists per tool
- `quicksuite/openapi_spec.json` — OpenAPI spec for Quick Suite import

**Docs:**
- `docs/architecture.md` — full architecture overview with ASCII diagrams
- `docs/setup-bedrock.md` — Bedrock setup (IAM, model access)
- `docs/setup-anthropic.md` — Anthropic direct API setup
- `docs/setup-openai.md` — OpenAI setup with site license instructions
- `docs/setup-gemini.md` — Gemini setup with Google Workspace instructions
- `docs/quicksuite-integration.md` — AgentCore Gateway + Quick Suite MCP setup

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
    │ MCP Actions Integration
    ▼
AgentCore Gateway (MCP server)
    │ HTTPS
    ▼
API Gateway + Cognito (OAuth 2.0 client_credentials)
    │
    ▼
Router Lambda
    ├── DynamoDB cache (optional, TTL-based)
    ├── Task classification (analyze/generate/research/summarize/code)
    ├── Provider selection (config-driven preference list)
    └── Fallback on error
         │
    ┌────┴────────┬──────────┬──────────┐
    ▼             ▼          ▼          ▼
Bedrock       Anthropic   OpenAI    Gemini
(Converse     (Messages   (Chat     (GenerativeAI
 API, IAM)     API)       Compl.)    API)
    │             │          │          │
    └─────────────┴────┬─────┴──────────┘
                       ▼
              Bedrock Guardrails (input + output)
              CloudWatch Metrics (per-provider tokens, latency)
              CloudTrail (automatic via API Gateway)
```

## Key Design Decisions

1. **Task-oriented tools, not provider-oriented.** Quick Suite users see
   `analyze`, `generate`, `research`, `summarize`, `code` — not "call
   Claude" or "call GPT". The router picks the best available provider.

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
   activates when temperature ≤ 0.3 to avoid caching nondeterministic
   responses. Callers can also set `skip_cache: true`.

7. **Secrets Manager for credentials, not env vars.** Provider Lambdas
   receive the secret ARN, not the key. Keys are fetched at runtime and
   cached in Lambda memory for the execution lifetime.

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
