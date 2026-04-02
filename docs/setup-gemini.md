# Setup: Google Gemini

Connect your Google AI API key — or your institution's Google AI
Enterprise credentials — to access Gemini models through Quick Suite
with full AWS governance.

## When to Use Gemini

- Your university has a Google Workspace for Education agreement
  that includes Gemini / AI Enterprise features
- Researchers are already using Gemini and you want to bring those
  workflows under governance
- You want Gemini's multimodal capabilities or long-context window
  as a routing option
- Gemini Flash is excellent for high-volume, cost-sensitive
  summarization tasks

**Key value**: Same as OpenAI — your existing Google relationship
becomes the on-ramp to AWS. Every Gemini call passes through Bedrock
Guardrails and CloudTrail. The governance layer is provider-agnostic.

## Prerequisites

- A Google AI API key (from [aistudio.google.com](https://aistudio.google.com/))
- Or: Google Cloud project with Gemini API enabled
- The CDK stack deployed

## Step 1: Get Your API Key

### Google AI Studio (Quickest)
1. Go to [aistudio.google.com](https://aistudio.google.com/)
2. Click **Get API key**
3. Create a key in a new or existing project
4. Copy the key (starts with `AIza`)

### Google Cloud (Enterprise)
1. Enable the **Generative Language API** in your GCP project
2. Go to **APIs & Services → Credentials**
3. Create an API key
4. Optionally restrict the key to the Generative Language API

### University Google Workspace
If your institution manages Google AI access centrally:
1. Contact your Google Workspace admin
2. Request a Generative Language API key provisioned under the
   institutional project
3. Confirm which Gemini models are available (Pro, Flash, etc.)

## Step 2: Store in Secrets Manager

```bash
# Get the secret ARN
SECRET_ARN=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteRouterStack \
  --query "Stacks[0].Outputs[?OutputKey=='GeminiSecretArn'].OutputValue" \
  --output text)

# Store your API key
aws secretsmanager put-secret-value \
  --secret-id "$SECRET_ARN" \
  --secret-string '{"api_key": "AIzaSy_YOUR_KEY_HERE"}'
```

## Step 3: Verify

Wait for a Lambda cold start or force refresh. Check `/status` to
confirm Gemini shows as available.

## Supported Models

| Model | ID | Notes |
|-------|----|-------|
| Gemini 2.5 Pro | `gemini-2.5-pro` | Strongest reasoning, long context |
| Gemini 2.5 Flash | `gemini-2.5-flash` | Fast, cost-effective |
| Gemini 2.0 Flash | `gemini-2.0-flash` | Previous gen, very fast |

## Routing Config — Google-First

For institutions where the Google relationship is primary:

```yaml
routing:
  analyze:
    preferred:
      - gemini/gemini-2.5-pro
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0
  research:
    preferred:
      - gemini/gemini-2.5-pro           # Long context window
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0
  summarize:
    preferred:
      - gemini/gemini-2.5-flash         # Fast and cheap
      - bedrock/amazon.nova-pro-v1:0
  generate:
    preferred:
      - gemini/gemini-2.5-pro
      - openai/gpt-4o
  code:
    preferred:
      - gemini/gemini-2.5-pro
      - bedrock/anthropic.claude-sonnet-4-20250514-v1:0
```

## Cost

Billed to your Google AI / GCP account. Gemini Flash in particular
is extremely cost-effective for summarization workloads.

## Notes

- Gemini's built-in safety filters are set to permissive mode in the
  provider Lambda because Bedrock Guardrails handle content filtering
  upstream. This avoids double-blocking.
- The Gemini API key is passed as a query parameter (Google's standard
  auth method for the Generative Language API). The Lambda function
  handles this — keys are never exposed to the client.
