# Setup: Anthropic (Direct API)

Connect your Anthropic API key to access Claude models directly —
bypassing Bedrock for access to the latest models or to use an
existing organizational agreement.

## When to Use Direct Anthropic

- You need a Claude model not yet available on Bedrock
- Your organization has a direct Anthropic contract
- You want access to Anthropic-specific features (extended thinking, etc.)
- Bedrock Claude is your primary but you want a direct fallback

## Prerequisites

- An Anthropic API key (from [console.anthropic.com](https://console.anthropic.com/))
- The CDK stack deployed

## Step 1: Get Your API Key

1. Log in to the [Anthropic Console](https://console.anthropic.com/)
2. Go to **API Keys**
3. Create a new key or use an existing one
4. Copy the key (starts with `sk-ant-`)

## Step 2: Store in Secrets Manager

```bash
# Get the secret ARN from CDK outputs
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteRouterStack \
  --query "Stacks[0].Outputs[?OutputKey=='AnthropicSecretArn'].OutputValue" \
  --output text)

# Store your API key
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ARN" \
  --secret-string '{"api_key": "sk-ant-YOUR_KEY_HERE"}'
```

## Step 3: Verify

The router checks for configured providers on cold start. Force a
refresh by updating the router Lambda's environment:

```bash
# Force router to re-check available providers
aws lambda update-function-configuration \
  --function-name qs-model-router-router \
  --environment "Variables={$(aws lambda get-function-configuration \
    --function-name qs-model-router-router \
    --query 'Environment.Variables' --output json | \
    python3 -c 'import sys,json; d=json.load(sys.stdin); d["_REFRESH"]=__import__("time").strftime("%s"); print(",".join(f"{k}={v}" for k,v in d.items()))')}"
```

Or simply wait for the next Lambda cold start (happens naturally).

Check the status endpoint to confirm Anthropic appears as available.

## Supported Models

| Model | ID | Notes |
|-------|----|-------|
| Claude Sonnet 4 | `claude-sonnet-4-20250514` | Default for most tasks |
| Claude Opus 4 | `claude-opus-4-20250514` | Strongest reasoning |
| Claude Haiku 3.5 | `claude-3-5-haiku-20241022` | Fast and cheap |

## Routing Config

In `config/routing_config.yaml`, the `anthropic/` prefix routes to this
provider. To prioritize direct Anthropic over Bedrock:

```yaml
routing:
  analyze:
    preferred:
      - anthropic/claude-sonnet-4-20250514    # Direct first
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0  # Bedrock fallback
```

## Cost

Billed directly to your Anthropic account. Token pricing at
[anthropic.com/pricing](https://www.anthropic.com/pricing).
The router tracks token usage in CloudWatch so you can monitor
spend alongside Bedrock costs.

## Removing

To disable the Anthropic provider, clear the secret:

```bash
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ARN" \
  --secret-string '{"api_key": ""}'
```

The router will skip Anthropic on next cold start and fall through
to the next provider in the preference list.
