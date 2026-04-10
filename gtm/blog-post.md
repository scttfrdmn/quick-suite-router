# Your University Already Has an AI Subscription. Here's How to Make It Actually Work.

Every university I talk to is buying AI. They have an OpenAI site license, a Google Workspace agreement that includes Gemini, or researchers charging Anthropic API keys to their grants. Sometimes all three.

And every time I mention Amazon Quick Suite — AWS's agentic workspace that combines research, business intelligence, and automation — I hear the same thing:

*"We already have an AI tool."*

Fair enough. But here's the question I ask next: **Who's governing it?**

The typical answer involves a long pause, followed by something about "acceptable use policies" that nobody reads. The CIO doesn't know which models researchers are using. The compliance team can't tell you whether sensitive research data is being pasted into ChatGPT. The finance office sees a line item for "AI subscriptions" but has no idea what they're getting for it.

This is the gap. Not the AI — the governance around it. And it's the gap that the Quick Suite Model Router was built to close.

## The problem isn't the model. It's the wrapper.

Quick Suite is a powerful platform. It connects to your enterprise data, provides AI-powered business intelligence through QuickSight, automates workflows through Flows, and gives every user an agentic chat interface. But its built-in LLM is a black box you can't change, and for many research-intensive tasks, users want the models they already know — Claude for deep reasoning, GPT-4o for generation, Gemini for long-context analysis.

Today, using those models means leaving Quick Suite. Researchers open a separate browser tab, paste their data into a commercial chat interface, get their answer, and paste it back. No audit trail. No content filtering. No cost visibility. No integration with the rest of their workflow.

The model router changes this. It's a CDK-deployable reference architecture — open source, one command to deploy — that connects Quick Suite to multiple LLM providers through Bedrock AgentCore Gateway. Your researchers keep using the models they know. Your institution gains the governance it needs. And it all happens inside Quick Suite's unified workspace.

## How it works

The architecture is deliberately simple. Quick Suite connects to a Bedrock AgentCore Gateway endpoint via MCP (Model Context Protocol) Actions Integration. Behind that gateway sits a lightweight routing layer — a set of Lambda functions that classify tasks, select the best available provider, and normalize responses into a common format.

The router exposes five task-oriented tools to Quick Suite:

- **Analyze** — deep analysis of documents, data, and concepts
- **Generate** — content creation: reports, emails, proposals
- **Research** — multi-source synthesis and literature review
- **Summarize** — fast, cost-effective summarization
- **Code** — code generation, review, and debugging

Users don't pick a provider. They describe what they need, and the router selects the best available model based on a configurable preference list. Analysis tasks might route to Claude on Bedrock. Summarization might go to Amazon Nova for speed and cost. Code tasks might hit GPT-4o through the university's OpenAI site license.

Each task type has a preference chain with automatic fallback. If the primary provider is rate-limited or unavailable, the router tries the next one. Users see a response; they don't see the provider switch.

## Four providers, one governance plane

The router supports four provider backends out of the box:

**Amazon Bedrock** is the default. It uses IAM authentication — no additional credentials needed. You get Claude, Nova, Llama, Mistral, and every other Bedrock-hosted model through a unified Converse API.

**Anthropic direct** connects to the Anthropic Messages API. Use this for access to the latest Claude models that may not yet be on Bedrock, or when your organization has a direct Anthropic agreement.

**OpenAI direct** connects to the Chat Completions API. This is designed specifically for organizations with existing site licenses. Your IT department drops the API key and organization ID into AWS Secrets Manager, and GPT-4o becomes available as a routing target inside Quick Suite. The university's existing OpenAI spend doesn't change — what changes is that every call now passes through AWS governance.

**Google Gemini direct** connects to the Generative AI API. For universities with Google Workspace for Education agreements that include Gemini, this brings their existing Google AI investment into the same governed workspace. Gemini Flash is particularly effective as a high-volume, cost-effective summarization backend.

Here's the part that matters: **every call, regardless of which provider handles it, passes through the same governance layer.** Bedrock Guardrails filter both input and output for content safety, prompt injection, and PII leakage. CloudWatch captures per-provider, per-model token counts and latency metrics. CloudTrail logs every invocation with full audit detail.

When a researcher sends a query to GPT-4o through their university's site license, that call hits Bedrock Guardrails before it leaves the AWS environment. Social Security numbers are blocked. Email addresses are anonymized. And the entire interaction shows up in CloudTrail — who asked what, when, through which model, and how many tokens it consumed.

This is the governance that doesn't exist when people use ChatGPT in a browser tab.

## The dashboard your CIO actually wants

The router deploys a pre-built CloudWatch dashboard that answers the questions every IT leader asks:

**Who's using what?** Invocation counts broken down by provider — Bedrock, OpenAI, Gemini, Anthropic. You can see at a glance whether your OpenAI site license is being utilized or whether everyone's defaulting to Bedrock.

**How much does it cost?** Token usage by provider over time. Input and output tokens tracked separately, because output tokens cost more. You can calculate exact spend by multiplying against each provider's published rates.

**Is the governance working?** Guardrail block counts show content policy in action. If a researcher tries to send a prompt containing PII, the guardrail catches it before it reaches any external provider. The block shows up in the dashboard, not in the researcher's data.

**How fast are responses?** Latency percentiles (p50 and p99) per provider. If OpenAI is consistently slower than Bedrock for the same task type, you have data to justify a routing change.

This dashboard is the artifact your CIO shows the board. It's the compliance evidence your IRB reviewer asks for. And it exists for every AI call across every provider, not just the ones going through Bedrock.

## The DynamoDB cache (and why it matters for cost)

The router includes an optional response cache backed by DynamoDB with TTL-based expiration. When a deterministic request (temperature ≤ 0.3) produces a response, that response is cached by a hash of the prompt, model, and system prompt.

This matters more than it sounds. In a university setting, many queries are structurally similar — students asking variations of the same research question, analysts running the same type of report weekly, departments generating templated content. The cache turns repeated work into instant responses at zero incremental token cost.

Deploy with the cache enabled (the default), or disable it with a single CDK context flag. Configure the TTL based on how fresh you need responses to be — an hour for research queries, a day for templated content, a week for reference material.

## Deployment: one command, fifteen minutes

The entire stack deploys with `cdk deploy`. It provisions:

- A Cognito User Pool for OAuth 2.0 authentication
- Secrets Manager entries for each provider (empty, ready to populate)
- Five Lambda functions (the router plus four providers)
- An API Gateway with Cognito authorization
- A Bedrock Guardrail with sensible defaults
- An optional DynamoDB cache table
- A CloudWatch usage dashboard

Infrastructure cost is roughly five dollars a month. The LLM token costs are whatever they'd be anyway — you're routing through governance, not adding a markup.

After deployment, configuring a provider is one AWS CLI command:

```
aws secretsmanager put-secret-value \
  --secret-id quicksuite-model-router/openai \
  --secret-string '{"api_key": "sk-...", "organization": "org-..."}'
```

Do this for each provider you want to enable. Bedrock requires nothing — it's always available through IAM. Providers without credentials are simply skipped in the routing chain.

Then register the API Gateway endpoint as an AgentCore Gateway target, create an MCP Actions in Quick Suite, and you're live. The full integration guide walks through every click.

## The strategic play

I'll be direct about why this matters to me professionally and why I think it matters for research computing.

Universities are not going to abandon their existing AI subscriptions. Those contracts represent real procurement cycles, real budget commitments, and real researcher workflows. Any solution that starts with "drop your OpenAI subscription" is dead on arrival.

But those subscriptions have a governance gap that's getting wider. Researchers are handling increasingly sensitive data — genomic sequences, medical records, export-controlled materials — and pasting it into commercial AI interfaces with no visibility or controls. The compliance risk is real and growing.

The model router turns the existing subscription into the on-ramp to a governed environment. The university keeps OpenAI. The university keeps Gemini. But now those calls pass through Bedrock Guardrails, show up in CloudTrail, and get metered in CloudWatch. The IT team gains visibility without disrupting the researchers.

And once the orchestration layer is running on AWS, something interesting happens. The routing config makes it trivial to A/B test providers. Put Claude on Bedrock first for analysis tasks, keep GPT-4o for generation, use Nova for cheap summarization. Compare quality and cost with real workloads — not benchmarks, not vendor claims, real production data flowing through the same governance pipeline.

In my experience, Bedrock wins most of those comparisons on cost and latency. When it does, the migration is a one-line YAML change. No re-architecture, no new integration, no new procurement cycle. The researcher's workflow doesn't change at all — they still ask Quick Suite to analyze their document. The only difference is which model processes the request.

That's the value of building on an open routing layer instead of a locked-in single-vendor AI product: you can evolve your model strategy as the market evolves, without touching the user experience or the governance infrastructure.

## What's next

The project is open source under Apache 2.0. The repository includes the full CDK stack, all four provider implementations, routing configuration, an OpenAPI spec for Quick Suite import, step-by-step setup guides for each provider, and an architecture overview.

It is version one. There are known limitations: synchronous only (no streaming), single-turn per invocation, and a global routing config rather than per-department overrides. These are on the roadmap.

But the core of the thing — bring your own model into Quick Suite with AWS governance on every call — works today, deploys in fifteen minutes, and costs less than a cup of coffee per month to run.

If your university has an AI subscription and a governance problem, this is the bridge between the two.

The repository is at [github.com/scttfrdmn/quicksuite-model-router](https://github.com/scttfrdmn/quicksuite-model-router).
