# Setup: Amazon Bedrock

Bedrock is the default provider and requires **no additional credentials** —
it uses your AWS account's IAM permissions.

## Prerequisites

- Bedrock model access enabled in your AWS account
- The CDK stack deployed (see main README)

## Enable Model Access

Before the Bedrock provider can invoke a model, you must request access
in the Bedrock console:

1. Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/)
2. In the left nav, choose **Model access**
3. Choose **Manage model access**
4. Enable the models you want to use:
   - **Anthropic** → Claude Sonnet 4 (recommended for analyze/generate/code)
   - **Amazon** → Nova Pro (recommended for summarize)
   - **Meta** → Llama 3.1 (optional, good alternative)
   - **Mistral** → Mistral Large (optional)
5. Choose **Request model access**
6. Access is typically granted within minutes

## Verify

After deploying the stack, check that Bedrock is available:

```bash
# Get the API endpoint from CDK outputs
API_URL=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteRouterStack \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
  --output text)

# Check provider status (requires auth — see quicksuite-integration.md)
curl -s "$API_URL/health"
# Expected: {"status": "healthy"}
```

## Supported Models

The default routing config uses these Bedrock model IDs:

| Model | ID | Best For |
|-------|----|----------|
| Claude Sonnet 4 | `anthropic.claude-sonnet-4-20250514-v1:0` | Analysis, generation, code |
| Nova Pro | `amazon.nova-pro-v1:0` | Fast summarization |
| Llama 3.1 70B | `meta.llama3-1-70b-instruct-v1:0` | General tasks |

You can use any Bedrock-hosted model by updating the routing config.
The Bedrock provider uses the **Converse API**, which provides a unified
interface across all model families.

## Customizing

Edit `config/routing_config.yaml` to change which Bedrock models are
used for which tasks. For example, to use Nova for everything:

```yaml
routing:
  analyze:
    preferred:
      - bedrock/amazon.nova-pro-v1:0
  generate:
    preferred:
      - bedrock/amazon.nova-pro-v1:0
  # ... etc
```

## Cost

Bedrock model pricing varies. See
[Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/).
Token costs appear on your AWS bill alongside the router infrastructure.

## Guardrails

The Bedrock provider integrates directly with the Bedrock Guardrail
deployed by the stack. Both input and output are filtered. The guardrail
ID is passed to the Converse API, so filtering happens server-side
within Bedrock — no extra round trip.
