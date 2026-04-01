# Quick Suite Model Router

**Bring your own LLM to Amazon Quick Suite.**

A CDK-deployable reference architecture that extends Quick Suite with
multi-provider LLM access through Bedrock AgentCore Gateway. Bring your
existing OpenAI, Anthropic, or Google Gemini credentials into Quick Suite
with full AWS governance — Bedrock Guardrails, CloudTrail audit, and
CloudWatch cost visibility — applied to every call regardless of provider.

## Why

Quick Suite ships with a built-in LLM that you can't change. Universities
and enterprises often have existing AI subscriptions (OpenAI site licenses,
Google AI Enterprise, Anthropic agreements) that represent significant
investments. Today, using those models means leaving Quick Suite — losing
the integrated workspace, BI, automation, and governance.

The model router solves this: **your existing AI subscription becomes
the on-ramp to Quick Suite, not a competitor to it.**

## What You Get

- **Five task-oriented tools** in Quick Suite: `analyze`, `generate`,
  `research`, `summarize`, `code`
- **Four LLM providers**: Bedrock (Claude, Nova, Llama), Anthropic direct,
  OpenAI direct, Google Gemini direct
- **Smart routing**: each task type routes to the best available model
  with automatic fallback
- **Unified governance**: Bedrock Guardrails on every call, CloudWatch
  usage metrics, CloudTrail audit — even for OpenAI and Gemini calls
- **Response cache**: optional DynamoDB cache with configurable TTL
- **One CDK command**: deploys in 15 minutes, ~$5/month infrastructure

## Quick Start

```bash
git clone https://github.com/scttfrdmn/quicksuite-model-router.git
cd quicksuite-model-router

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/routing_config.example.yaml config/routing_config.yaml

cdk bootstrap   # first time only
cdk deploy
```

Then configure your providers:

```bash
# OpenAI (e.g., university site license)
aws secretsmanager put-secret-value \
  --secret-id quicksuite-model-router/openai \
  --secret-string '{"api_key": "sk-...", "organization": "org-..."}'

# Anthropic
aws secretsmanager put-secret-value \
  --secret-id quicksuite-model-router/anthropic \
  --secret-string '{"api_key": "sk-ant-..."}'

# Google Gemini
aws secretsmanager put-secret-value \
  --secret-id quicksuite-model-router/gemini \
  --secret-string '{"api_key": "AIza..."}'
```

Bedrock is available immediately (IAM-based, no key needed).

Connect to Quick Suite via AgentCore Gateway — see
[Quick Suite Integration Guide](docs/quicksuite-integration.md).

## Architecture

```
Quick Suite → AgentCore Gateway (MCP) → API Gateway → Router Lambda
                                                          │
                                            ┌─────────────┼─────────────┐
                                            ▼             ▼             ▼
                                        Bedrock       OpenAI       Gemini
                                        (Claude,    (GPT-4o,     (Pro,
                                         Nova,      o3, mini)    Flash)
                                         Llama)
                                            │             │            │
                                            └──────┬──────┘────────────┘
                                                   ▼
                                          Bedrock Guardrails
                                          CloudWatch Metrics
                                          CloudTrail Audit
```

See [Architecture](docs/architecture.md) for the full design.

## Provider Setup Guides

| Provider | Guide | Auth | Notes |
|----------|-------|------|-------|
| Amazon Bedrock | [Setup](docs/setup-bedrock.md) | IAM (zero config) | Claude, Nova, Llama, Mistral |
| Anthropic | [Setup](docs/setup-anthropic.md) | API key | Direct Claude access |
| OpenAI | [Setup](docs/setup-openai.md) | API key + org | Site license support |
| Google Gemini | [Setup](docs/setup-gemini.md) | API key | Workspace integration |

## Routing Configuration

Edit `config/routing_config.yaml` to control provider preference per task:

```yaml
routing:
  analyze:
    preferred:
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0  # Try Bedrock first
      - openai/gpt-4o                                     # Fallback to site license
      - gemini/gemini-2.5-pro                              # Then Gemini
  summarize:
    preferred:
      - bedrock/amazon.nova-pro-v1:0     # Fast and cheap
      - openai/gpt-4o-mini               # Also fast and cheap
```

The router tries providers in order and automatically falls back if one
is unavailable or returns an error. Providers without configured
credentials are skipped.

To force a single provider for everything, just put it first in every
list. To let a university use their OpenAI site license for primary
inference, put `openai/` first.

## Deployment Options

```bash
# Standard (with response cache)
cdk deploy

# Without cache
cdk deploy -c enable_cache=false

# Custom cache TTL (2 hours)
cdk deploy -c cache_ttl_minutes=120

# Specific region
cdk deploy -c region=us-west-2
```

## For Account Teams

If you're a BD or AM working with universities and research institutions:

- **[BD Playbook](gtm/bd-playbook.md)** — discovery questions, demo
  script, competitive positioning
- **[AM Talking Points](gtm/am-talking-points.md)** — the one-liner,
  stakeholder-specific messaging, value chain
- **[Objection Handling](gtm/objection-handling.md)** — "we already
  have OpenAI" and every other objection, with responses

## Cost

| Component | Monthly Cost |
|-----------|-------------|
| Lambda functions | ~$0.50 (at typical usage) |
| API Gateway | ~$1-3 |
| DynamoDB cache | ~$0.50 |
| Secrets Manager | $1.20 (3 secrets) |
| CloudWatch | ~$1-2 |
| **Infrastructure total** | **~$5/month** |
| LLM tokens | Per-provider pricing (your existing spend) |

## License

Apache 2.0
