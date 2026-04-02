# Setup: OpenAI

Connect your existing OpenAI subscription — including university site
licenses — to access GPT models through Quick Suite with full AWS
governance.

## When to Use OpenAI

- Your university or organization has an OpenAI site license or
  enterprise agreement
- Researchers are already using GPT models and you want to bring
  their workflows into a governed environment
- You want GPT-4o or o3 as a routing option alongside Claude

**Key value**: Your OpenAI spend doesn't change. What changes is that
every call now passes through Bedrock Guardrails, gets metered in
CloudWatch, and shows up in CloudTrail. You get governance for free
over an investment you've already made.

## Prerequisites

- An OpenAI API key (from [platform.openai.com](https://platform.openai.com/))
- Optional: Organization ID (for enterprise/site license accounts)
- The CDK stack deployed

## Step 1: Get Your Credentials

### Standard API Key
1. Log in to [platform.openai.com](https://platform.openai.com/)
2. Go to **API keys**
3. Create a new key
4. Copy the key (starts with `sk-`)

### University Site License
If your institution has an OpenAI site license:
1. Contact your IT department for the organization's API key
2. Get the Organization ID (starts with `org-`) — found under
   **Settings → Organization**
3. Some licenses have specific allowed models — confirm GPT-4o access

## Step 2: Store in Secrets Manager

```bash
# Get the secret ARN from CDK outputs
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteRouterStack \
  --query "Stacks[0].Outputs[?OutputKey=='OpenaiSecretArn'].OutputValue" \
  --output text)

# Standard key
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ARN" \
  --secret-string '{"api_key": "sk-YOUR_KEY_HERE"}'

# With organization ID (site license)
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ARN" \
  --secret-string '{"api_key": "sk-YOUR_KEY", "organization": "org-YOUR_ORG"}'
```

## Step 3: Verify

Wait for a Lambda cold start or force a refresh (see Anthropic setup
guide for the refresh command). Check the `/status` endpoint to confirm
OpenAI shows as available.

## Supported Models

| Model | ID | Notes |
|-------|----|-------|
| GPT-4o | `gpt-4o` | Strong all-around, default |
| GPT-4o mini | `gpt-4o-mini` | Fast/cheap, good for summarize |
| o3 | `o3` | Reasoning model (if available on your plan) |
| o4-mini | `o4-mini` | Cost-effective reasoning |

## Routing Config — Site License Priority

If your university has an OpenAI site license and you want GPT to be
the default for everything:

```yaml
routing:
  analyze:
    preferred:
      - openai/gpt-4o                           # Your site license
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0  # Bedrock fallback
  generate:
    preferred:
      - openai/gpt-4o
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0
  summarize:
    preferred:
      - openai/gpt-4o-mini                       # Cheap and fast
      - bedrock/amazon.nova-pro-v1:0
  code:
    preferred:
      - openai/gpt-4o
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0
```

This configuration means: use the OpenAI site license for primary
inference (no incremental cost to the department), fall back to Bedrock
if OpenAI is rate-limited or unavailable.

## Cost

Billed to your OpenAI account. If using a site license, usage may
be covered under the institutional agreement — check with your IT
procurement team.

The router emits per-provider CloudWatch metrics, so you can report
OpenAI token consumption separately from Bedrock spend.

## Rate Limits

OpenAI enforces rate limits per organization. The router handles 429
responses gracefully — it will automatically fall back to the next
provider in the preference list. If you see frequent fallbacks in the
CloudWatch dashboard, you may need to request a rate limit increase
from OpenAI.
