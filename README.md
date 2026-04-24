# Quick Suite Router

**Bring your existing AI subscriptions into any agent connected through Amazon Bedrock AgentCore Gateway — with full AWS governance on every call.**

Universities and research institutions typically have existing AI agreements — an OpenAI
site license, an Anthropic enterprise subscription, or Google AI through a Workspace
agreement. Without this router, using those models means stepping outside the governed
environment: a separate browser tab, no BI integration, no workflow automation, and no
centralized governance over what the models say.

The Router solves this by registering six task-oriented tools through AgentCore Gateway
and routing each request to the best available model from any of your configured
providers. Users of any connected agent (Quick Suite, Kiro, custom) never need to know
or choose which model answered — they just work. IT and compliance get a single audit
trail and Bedrock Guardrails governance on every response, regardless of which provider
generated it.

## What most agents can't do without this router

- Prefer a specific model for a specific task type (e.g., Claude for research synthesis, GPT-4o for code generation)
- Match tasks to models by required capabilities (structured output, long context, vision) and context window budget
- Fall back to a secondary provider automatically when the primary is slow or unavailable
- Apply Bedrock Guardrails governance to responses from Anthropic, OpenAI, or Gemini
- Track token usage, cost, and spend per department in CloudWatch and DynamoDB
- Route different departments to different providers (the law school prefers one model; engineering prefers another)
- Restrict PHI-tagged requests to Bedrock only — non-Bedrock providers never see PHI
- Extract structured data (effect sizes, methods, citations) from scientific text
- Use an existing university site license within the Quick Suite workspace

## What You Get

**Six task-oriented tools** in Quick Suite: `analyze`, `generate`, `research`,
`summarize`, `code`, `extract`

**Four LLM providers**: Amazon Bedrock (Claude, Nova, Llama), Anthropic direct, OpenAI
direct, Google Gemini direct — any combination, any order

**Configurable routing**: set your preferred provider order per task type; the router
tries them in order and falls back automatically on error or timeout

**Capability routing**: match tasks to models by required capabilities and context window
budget; models missing a required capability or with insufficient context are skipped
automatically

**Department overrides**: different departments can route to different providers — set in
a YAML config, no code changes

**Unified governance**: Bedrock Guardrails applied to every response regardless of which
provider generated it; CloudWatch usage metrics per provider; CloudTrail audit

**Spend tracking**: per-department budget caps with a DynamoDB spend ledger; `query_spend`
AgentCore Lambda target for finance teams; Cognito JWT-based authorization restricts
non-admin callers to their own department

**Response cache**: optional DynamoDB cache with configurable TTL, activated for
low-temperature requests (temperature ≤ 0.3) to avoid re-billing the same question

**Multi-turn conversations**: conversation history formatted correctly for each provider's
native API (messages array for Anthropic/OpenAI, contents array with role mapping for Gemini)

**SSE streaming**: `generate` and `research` tools support `stream: true`; buffered-streaming
pattern returns `chunks` list + assembled `content`; guardrails applied to assembled text;
all four providers supported

**Structured extraction**: the `extract` tool pulls effect sizes, methods, confounds, open
problems, and citations from scientific text; providers enable native JSON mode (OpenAI:
`response_format`, Gemini: `responseMimeType`); `open_problems` extraction type persists
gap lists to S3 for clAWS watch consumption

**Grounding mode**: `research` with `grounding_mode: "strict"` returns `sources_used`,
`grounding_coverage`, and `low_confidence_claims` for verifiable, citation-backed answers

**PHI routing**: requests tagged `data_classification: "phi"` are silently restricted to
Bedrock only; non-Bedrock providers never receive PHI; returns 503 if no Bedrock available

**Dry-run mode**: `dry_run: true` returns estimated cost, selected provider, selected model,
and token estimate without invoking any model or writing to the spend ledger

**Per-user rate limiting**: Cognito JWT-based Lambda authorizer with configurable
requests-per-minute and daily quotas via API Gateway usage plans

**VPC isolation**: optional `enable_vpc` deployment places all Lambdas in a private VPC
with no internet egress; Gateway endpoints for S3/DynamoDB; Interface endpoints for
Secrets Manager, Lambda, CloudWatch, X-Ray, Bedrock

**Content audit logging**: SHA-256 hashes of prompts and responses when
`enable_content_logging=true` for compliance review without storing raw content

**Guardrail version management**: SSM Parameter Store for guardrail version; updater Lambda
changes the version without redeployment

## Architecture

```
Quick Suite conversation
        │  MCP Actions
        ▼
AgentCore Gateway (OpenAPI target)
        │
        ▼
API Gateway + Cognito (OAuth 2.0 client_credentials)
        │
    ┌───┴───────────────────────────────────────┐
    │                                           │
    ▼                                           ▼
Router Lambda                            query-spend Lambda
    │                                    (AgentCore target)
    ├── Capability + context matching
    ├── DynamoDB cache (optional)
    ├── Spend ledger write + budget check
    │
    ┌───────────┼───────────┬──────────────┐
    ▼           ▼           ▼              ▼
 Bedrock     Anthropic    OpenAI        Gemini
 (Claude,    (direct)     (direct)      (direct)
  Nova,
  Llama)
    │           │           │              │
    └───────────┴─────┬─────┴──────────────┘
                      ▼
             Bedrock Guardrails
             (applied to every response)
             CloudWatch Metrics
             CloudTrail Audit
```

The Router is the one component that deploys behind API Gateway rather than as a direct
Lambda target — because AgentCore Gateway's OpenAPI target type needs an HTTP endpoint.
All other Quick Suite extensions (Data, Compute, clAWS) invoke their Lambdas directly.

The Gateway ID from this stack's outputs is what the other extensions need for their
shared-Gateway configuration.

## Quick Start

```bash
git clone https://github.com/scttfrdmn/campus-router.git
cd campus-router

uv sync --extra dev --extra cdk   # or: pip install -r requirements.txt

cp config/routing_config.example.yaml config/routing_config.yaml
# Edit routing_config.yaml to set your provider preferences (see below)

cdk bootstrap   # first time only, per account/region
cdk deploy
```

Configure credentials for the providers you want to use:

```bash
# OpenAI (e.g., a university site license)
aws secretsmanager put-secret-value \
  --secret-id qs-router/openai \
  --secret-string '{"api_key": "sk-...", "organization": "org-..."}'

# Anthropic
aws secretsmanager put-secret-value \
  --secret-id qs-router/anthropic \
  --secret-string '{"api_key": "sk-ant-..."}'

# Google Gemini
aws secretsmanager put-secret-value \
  --secret-id qs-router/gemini \
  --secret-string '{"api_key": "AIza..."}'
```

Bedrock is available immediately — it uses your IAM role, no API key needed.

After deploying, register the API Gateway endpoint as an AgentCore Gateway OpenAPI target.
The CDK output `GatewayEndpointUrl` is the URL to register.

## Routing Configuration

`config/routing_config.yaml` controls which provider handles each task type, defines
department-specific overrides, and declares model capabilities:

```yaml
routing:
  analyze:
    preferred:
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0  # Try Bedrock first
      - openai/gpt-4o                                     # Fall back to site license
      - gemini/gemini-2.5-pro                             # Then Gemini
  summarize:
    preferred:
      - bedrock/amazon.nova-pro-v1:0   # Fast and inexpensive
      - openai/gpt-4o-mini
  code:
    preferred:
      - openai/gpt-4o
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0

department_overrides:
  law-school:
    analyze:
      preferred:
        - bedrock/anthropic.claude-opus-4-20250514-v1:0  # More reasoning for legal analysis
  engineering:
    code:
      preferred:
        - openai/gpt-4o

model_capabilities:
  bedrock/anthropic.claude-sonnet-4-20250514-v1:0:
    - structured_output
    - vision
    - long_context
  openai/gpt-4o:
    - structured_output
    - vision

model_context_windows:
  bedrock/anthropic.claude-sonnet-4-20250514-v1:0: 200000
  openai/gpt-4o: 128000
  gemini/gemini-2.5-pro: 1000000
```

Providers listed without configured credentials are silently skipped — so you can keep
the same config file across deployments that have different credentials.

## Deployment Options

```bash
cdk deploy                                        # standard
cdk deploy -c enable_cache=false                  # disable response cache
cdk deploy -c cache_ttl_minutes=120               # 2-hour cache TTL (default: 60)
cdk deploy -c enable_vpc=true                     # VPC isolation (no internet egress)
cdk deploy -c cors_allowed_origin=https://...     # restrict CORS origin
cdk deploy -c rate_limit_per_minute=60            # per-user rate limiting
cdk deploy -c rate_limit_per_day=1000             # per-user daily quota
cdk deploy -c enable_content_logging=true         # SHA-256 audit hashes
cdk deploy -c region=us-west-2                    # specific region
```

## Cross-Stack Integration

The Router's spend ledger (`qs-router-spend` DynamoDB table) is read by the Compute
extension before submitting jobs — preventing budget overruns across both model inference
and compute. Set `router_spend_table_arn` in Compute's CDK context to enable this.

The `extract` tool with `open_problems` type writes gap lists to S3 that clAWS's
`cross_discipline` watch consumes for adjacent-field paper detection.

The `summarize` and `research` tools are called by clAWS watch runners to score findings
and draft briefings.

## Cost

| Component | Monthly Cost |
|-----------|-------------|
| Lambda invocations | ~$0.50 |
| API Gateway | ~$1–3 |
| DynamoDB (cache + spend ledger) | ~$1.00 |
| Secrets Manager (3 secrets) | ~$1.20 |
| CloudWatch | ~$1–2 |
| **Infrastructure total** | **~$5/month** |
| LLM tokens | Your existing provider spend — no markup |

## Known Limitations

**Guardrails apply differently by provider.** For Bedrock, guardrails block inside the
model call before any tokens are returned. For Anthropic, OpenAI, and Gemini, guardrails
run as a post-call check — the provider call happens first, then the output is evaluated.
You may incur token costs from those providers even when a guardrail ultimately blocks
the response.

**Cache scope.** The DynamoDB cache only activates at `temperature ≤ 0.3`. It is not
automatically invalidated when you rotate an API key — use `skip_cache: true` on the
first call after a rotation.

**Single-region deployment.** For high availability across regions, deploy two stacks and
put Route 53 latency routing in front of both API Gateway endpoints.

**API Gateway ceiling.** The router sits behind API Gateway, which hard-kills connections
after 29 seconds regardless of the Lambda's own timeout. External provider calls
(Anthropic, OpenAI, Gemini) are capped at 25 seconds to stay safely inside this window;
slow provider calls trigger fallback to the next configured provider rather than a silent
connection drop. Bedrock calls use the SDK default and are typically well under 10s.

**Input limits.** Prompts are capped at 100 KB. Maximum output tokens: 16,384.

## Documentation

| Doc | What it covers |
|-----|---------------|
| [docs/architecture.md](docs/architecture.md) | Component map, Lambda internals, routing decision tree |
| [docs/compliance.md](docs/compliance.md) | HIPAA-ready deployment guide (VPC, PHI tagging, CloudTrail, Guardrail hardening) |
| [docs/setup-bedrock.md](docs/setup-bedrock.md) | Bedrock model access and IAM setup |
| [docs/setup-anthropic.md](docs/setup-anthropic.md) | Anthropic API key and organization ID |
| [docs/setup-openai.md](docs/setup-openai.md) | OpenAI API key and site license configuration |
| [docs/setup-gemini.md](docs/setup-gemini.md) | Google Gemini API key and Workspace setup |

## License

Apache-2.0 — Copyright 2026 Scott Friedman
