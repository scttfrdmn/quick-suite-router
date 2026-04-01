# Quick Suite Integration

How to connect the deployed model router to Amazon Quick Suite via
Bedrock AgentCore Gateway.

## Overview

The integration path is:

```
Quick Suite → AgentCore Gateway (MCP) → API Gateway → Router Lambda
```

You'll register the API Gateway endpoint as an AgentCore Gateway target,
then create an MCP Actions Integration in Quick Suite that connects to
AgentCore.

## Prerequisites

- The CDK stack deployed (`cdk deploy` completed)
- At least one provider configured (Bedrock is always available)
- Quick Suite admin access (Author Pro or Reader Pro tier)
- CDK output values (run `aws cloudformation describe-stacks --stack-name QuickSuiteModelRouter`)

## Step 1: Note Your CDK Outputs

After deployment, collect these values:

```bash
# All outputs at once
aws cloudformation describe-stacks \
  --stack-name QuickSuiteModelRouter \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" \
  --output table
```

You'll need:
- **ApiEndpoint** — the HTTP backend URL
- **CognitoClientId** — for authentication
- **CognitoTokenUrl** — token endpoint

The Cognito Client Secret is stored in Secrets Manager. Retrieve it:

```bash
# Get the client secret (needed for Quick Suite auth setup)
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteModelRouter \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text)

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name QuickSuiteModelRouter \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoClientId'].OutputValue" \
  --output text)

aws cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --query "UserPoolClient.ClientSecret" \
  --output text
```

## Step 2: Register with AgentCore Gateway

### Option A: Via AWS Console

1. Open the [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/)
2. Navigate to **AgentCore → Gateways**
3. Create a new gateway (or use an existing one)
4. Add a new **Target**:
   - **Type**: HTTP endpoint
   - **URL**: Your `ApiEndpoint` value
   - **Authentication**: OAuth 2.0 Client Credentials
   - **Token URL**: Your `CognitoTokenUrl`
   - **Client ID**: Your `CognitoClientId`
   - **Client Secret**: (from Step 1 above)
   - **Scope**: `model-router/invoke`
5. Save and note the **Gateway Resource URL** (the MCP server endpoint)

### Option B: Via OpenAPI Spec Import

1. Open AgentCore Gateway
2. Choose **Import OpenAPI Specification**
3. Upload `quicksuite/openapi_spec.json` from this repository
4. Configure authentication as in Option A
5. Review discovered tools and confirm

## Step 3: Connect Quick Suite

1. Log in to Amazon Quick Suite as an admin
2. Go to the **admin console** (profile icon → Manage Quick Suite)
3. Navigate to **Integrations → Actions**
4. Choose **New Action** → **MCP Integration**
5. Enter:
   - **Name**: `Multi-Model Router`
   - **Description**: `Access Claude, GPT, Gemini, and Nova through
     a unified interface with governance. Provides analyze, generate,
     research, summarize, and code tools.`
   - **Endpoint URL**: Your AgentCore Gateway Resource URL
6. Choose **Service authentication** (2LO)
7. Enter the Cognito credentials:
   - **Client ID**: Your `CognitoClientId`
   - **Client Secret**: (from Step 1)
   - **Token URL**: Your `CognitoTokenUrl`
8. Choose **Next**
9. Review the discovered tools:
   - `analyze` — Deep analysis
   - `generate` — Content generation
   - `research` — Research synthesis
   - `summarize` — Fast summarization
   - `code` — Code assistance
   - `status` — Provider status
10. Choose **Next** to configure sharing
11. Share with users/groups who should have access
12. Choose **Done**

## Step 4: Test

1. Open Quick Suite chat (My Assistant or a custom agent)
2. The model router tools should be available
3. Try a prompt that triggers the router:

   > Analyze the key differences between transformer and LSTM
   > architectures for sequence modeling tasks.

4. The response should include the model router's output, with the
   actual provider and model visible in the response metadata.

5. Check the status:

   > What providers are available in the model router?

   This invokes the `status` tool and shows which providers are
   configured.

## Step 5: Configure Chat Agents

For the best experience, create a dedicated chat agent:

1. In Quick Suite admin, go to **Chat Agents**
2. Create a new agent
3. Under **Actions**, add the `Multi-Model Router` integration
4. Set the agent persona to explain that it has access to multiple
   LLM providers for different task types
5. Share with your users

## Troubleshooting

**Tools not appearing in Quick Suite**
- Verify the MCP integration is active (green status) in admin console
- Check that the user has access to the integration (sharing settings)
- Verify the AgentCore Gateway endpoint is reachable

**Authentication errors**
- Confirm the Cognito domain was created: check the `CognitoTokenUrl`
  resolves
- Verify client ID and secret match
- Check that the OAuth scope includes `model-router/invoke`

**Provider not available**
- Check the status endpoint to see which providers are configured
- Verify the secret has been populated in Secrets Manager
- Check CloudWatch logs for the router Lambda

**Slow responses**
- First call after deployment may be slow (Lambda cold starts)
- Check if responses are being cached (the `cached` field in response)
- Consider provisioned concurrency for the router Lambda if latency
  is critical
