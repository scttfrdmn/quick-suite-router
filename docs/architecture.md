# Architecture

## Overview

The Quick Suite Model Router sits between Amazon Quick Suite and multiple
LLM providers, providing a unified task-oriented interface with AWS-native
governance applied to every call — regardless of which provider handles it.

```
┌─────────────────────────────────┐
│       Amazon Quick Suite        │
│   (Chat Agents, Flows, etc.)   │
└──────────────┬──────────────────┘
               │ MCP Actions Integration
               ▼
┌─────────────────────────────────┐
│   Bedrock AgentCore Gateway     │
│   (MCP server, tool discovery)  │
└──────────────┬──────────────────┘
               │ HTTPS
               ▼
┌─────────────────────────────────┐
│      API Gateway + Cognito      │
│   (HTTP backend, OAuth 2.0)     │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│        Router Lambda            │
│  ┌───────────┐  ┌────────────┐  │
│  │  Task     │  │  Response   │ │
│  │  Classify │  │  Cache      │ │
│  │  + Route  │  │  (DynamoDB) │ │
│  └─────┬─────┘  └────────────┘  │
└────────┼────────────────────────┘
         │ Invoke
    ┌────┼────────┬──────────┬──────────┐
    ▼    ▼        ▼          ▼          ▼
┌──────┐┌───────┐┌────────┐┌────────┐
│Bedrock││Anthro-││OpenAI  ││Gemini  │
│      ││pic    ││        ││        │
│Claude ││Direct ││Direct  ││Direct  │
│Nova   ││API   ││API     ││API     │
│Llama  ││      ││        ││        │
└──┬───┘└──┬────┘└───┬────┘└───┬────┘
   │       │         │         │
   └───────┴─────┬───┴─────────┘
                 ▼
    ┌─────────────────────────┐
    │    Governance Layer     │
    │  ┌────────┐ ┌────────┐  │
    │  │Bedrock │ │CloudW. │  │
    │  │Guard-  │ │Usage   │  │
    │  │rails   │ │Metrics │  │
    │  └────────┘ └────────┘  │
    │  ┌────────┐ ┌────────┐  │
    │  │Cloud-  │ │PII     │  │
    │  │Trail   │ │Redact  │  │
    │  └────────┘ └────────┘  │
    └─────────────────────────┘
```

## Components

### Router Lambda

The router receives task-oriented requests (`analyze`, `generate`,
`research`, `summarize`, `code`) and determines which provider to use
based on:

1. **Routing config** — a YAML preference list per tool type
2. **Provider availability** — which providers have credentials configured
3. **Explicit override** — the caller can force a specific provider
4. **Fallback chain** — if the preferred provider fails, try the next one

The router also manages the response cache (DynamoDB) and emits
usage metrics to CloudWatch.

### Provider Lambdas

Four thin wrappers (~80 lines each) that normalize each provider's API
into a common request/response schema:

- **Bedrock** — Converse API (Claude, Nova, Llama, Mistral via IAM)
- **Anthropic** — Messages API (direct, for latest models or org agreements)
- **OpenAI** — Chat Completions API (site license support)
- **Gemini** — Generative AI API (Google Workspace / AI Enterprise)

Each provider Lambda:
- Pulls credentials from Secrets Manager (except Bedrock, which uses IAM)
- Makes the vendor-specific API call
- Returns a normalized response with token counts and metadata

### Governance Layer

Applied to **every** provider call, including direct-to-OpenAI and
direct-to-Gemini:

- **Bedrock Guardrails** — Content filtering on both input and output.
  Blocks prompt injection, hate speech, PII leakage. Configurable per
  deployment.
- **CloudWatch Metrics** — Per-provider, per-model token counts, latency
  percentiles, error rates, guardrail blocks. Pre-built dashboard.
- **CloudTrail** — Every AgentCore Gateway invocation is automatically
  logged. Full audit trail of who asked what, when.
- **PII Redaction** — SSNs and credit card numbers blocked at the
  guardrail. Email and phone anonymized.

### Response Cache (Optional)

DynamoDB table with TTL-based expiration. Keyed on SHA-256 of
(model + system_prompt + prompt). Only caches low-temperature
(≤0.3) requests where deterministic responses are expected.

Deploy with or without cache:
```bash
# With cache (default)
cdk deploy

# Without cache
cdk deploy -c enable_cache=false

# Custom TTL (minutes)
cdk deploy -c cache_ttl_minutes=120
```

### Authentication

Cognito User Pool with OAuth 2.0 client_credentials flow. AgentCore
Gateway authenticates using a Cognito app client. Quick Suite connects
to AgentCore via MCP Actions Integration with the Cognito credentials.

The flow: Quick Suite → AgentCore Gateway → Cognito token exchange →
API Gateway (authorized) → Router Lambda.

## Cost Model

| Component | Cost Driver | Typical |
|-----------|-------------|---------|
| Router Lambda | Invocations + duration | ~$0.01/1000 calls |
| Provider Lambdas | Invocations + duration | ~$0.01/1000 calls |
| API Gateway | Requests | $3.50/million |
| Secrets Manager | Per secret/month | $0.40 × 3 = $1.20/mo |
| DynamoDB Cache | Storage + reads/writes | <$1/mo typical |
| Guardrails | Per assessment | Per Bedrock pricing |
| LLM tokens | Per provider pricing | Varies by model |

The infrastructure cost is negligible. The LLM token cost is what
it would be anyway — you're just routing through governance now.

## Security

- All secrets in AWS Secrets Manager (encrypted at rest with KMS)
- IAM least-privilege policies per Lambda
- Cognito OAuth 2.0 for API authentication
- VPC deployment optional (add to CDK stack if required)
- CloudTrail logging on all API Gateway calls
- No credentials stored in code or environment variables (ARNs only)
