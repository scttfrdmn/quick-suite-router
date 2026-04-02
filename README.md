# Quick Suite Router

**Bring your existing AI subscriptions into Amazon Quick Suite — with full AWS governance on every call.**

Quick Suite ships with one built-in language model. That's fine for many tasks, but
universities and research institutions typically have existing AI agreements — an OpenAI
site license, an Anthropic enterprise subscription, or Google AI through a Workspace
agreement. Today, using those models means leaving Quick Suite: a separate browser tab,
no BI integration, no workflow automation, and no centralized governance over what the
models say.

The Router solves this by registering five task-oriented tools in Quick Suite's chat
interface and routing each request to the best available model from any of your configured
providers. An analyst working in Quick Suite never needs to know or choose which model
answered — they just work. IT and compliance get a single audit trail and Bedrock
Guardrails governance on every response, regardless of which provider generated it.

## What Quick Suite Alone Can't Do Here

- Prefer a specific model for a specific task type (e.g., Claude for research synthesis, GPT-4o for code generation)
- Fall back to a secondary provider automatically when the primary is slow or unavailable
- Apply Bedrock Guardrails governance to responses from Anthropic, OpenAI, or Gemini
- Track token usage and cost per provider in CloudWatch
- Route different departments to different providers (the law school prefers one model; engineering prefers another)
- Use an existing university site license within the Quick Suite workspace

## What You Get

**Five task-oriented tools** in Quick Suite: `analyze`, `generate`, `research`, `summarize`, `code`

**Four LLM providers**: Amazon Bedrock (Claude, Nova, Llama), Anthropic direct, OpenAI
direct, Google Gemini direct — any combination, any order

**Configurable routing**: set your preferred provider order per task type; the router
tries them in order and falls back automatically on error or timeout

**Department overrides**: different departments can route to different providers — set in
a YAML config, no code changes

**Unified governance**: Bedrock Guardrails applied to every response regardless of which
provider generated it; CloudWatch usage metrics per provider; CloudTrail audit

**Response cache**: optional DynamoDB cache with configurable TTL, activated for
low-temperature requests (temperature ≤ 0.3) to avoid re-billing the same question

**Multi-turn conversations**: conversation history formatted correctly for each provider's
native API (messages array for Anthropic/OpenAI, contents array with role mapping for Gemini)

## Architecture

```
Quick Suite conversation
        │  MCP Actions
        ▼
AgentCore Gateway (OpenAPI target)
        │
        ▼
API Gateway → Router Lambda
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
shared-Gateway configuration (the `CLAWS_GATEWAY_ID` context variable).

## Quick Start

```bash
git clone https://github.com/scttfrdmn/quick-suite-router.git
cd quick-suite-router

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

`config/routing_config.yaml` controls which provider handles each task type and defines
any department-specific overrides:

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
```

Providers listed without configured credentials are silently skipped — so you can keep
the same config file across deployments that have different credentials.

## Deployment Options

```bash
cdk deploy                              # standard
cdk deploy -c enable_cache=false        # disable response cache
cdk deploy -c cache_ttl_minutes=120     # 2-hour cache TTL (default: 60)
cdk deploy -c region=us-west-2          # specific region
```

## Cost

| Component | Monthly Cost |
|-----------|-------------|
| Lambda invocations | ~$0.50 |
| API Gateway | ~$1–3 |
| DynamoDB (response cache) | ~$0.50 |
| Secrets Manager (3 secrets) | ~$1.20 |
| CloudWatch | ~$1–2 |
| **Infrastructure total** | **~$5/month** |
| LLM tokens | Your existing provider spend — no markup |

## Known Limitations

**No streaming.** All LLM calls complete before returning. Long-form generation will
feel slower than a streaming interface. This is a Lambda constraint; streaming support
would require a different transport architecture.

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

**Input limits.** Prompts are capped at 100 KB. Maximum output tokens: 16,384. Router
timeout: 30 seconds per call.

## Documentation

| Doc | What it covers |
|-----|---------------|
| [docs/architecture.md](docs/architecture.md) | Component map, Lambda internals, routing decision tree |
| [docs/setup-bedrock.md](docs/setup-bedrock.md) | Bedrock model access and IAM setup |
| [docs/setup-anthropic.md](docs/setup-anthropic.md) | Anthropic API key and organization ID |
| [docs/setup-openai.md](docs/setup-openai.md) | OpenAI API key and site license configuration |
| [docs/setup-gemini.md](docs/setup-gemini.md) | Google Gemini API key and Workspace setup |

## License

Apache-2.0 — Copyright 2026 Scott Friedman
