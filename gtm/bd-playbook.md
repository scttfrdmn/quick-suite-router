# BD Playbook: Quick Suite Model Router

## The Play

Universities and research institutions are buying AI. Most already have
subscriptions to OpenAI (ChatGPT Enterprise/Education), Google (Gemini
via Workspace), or both. When we pitch Quick Suite, the common objection
is: **"We already have an AI tool."**

The model router flips this: **"Great — bring it with you."**

We deploy a reference architecture that lets their existing OpenAI or
Gemini subscription work *inside* Quick Suite, wrapped in AWS governance.
They keep their investment. They gain a unified workspace, audit trail,
content guardrails, and cost visibility they don't have today.

Once the orchestration layer is running on AWS, switching from OpenAI
to Bedrock is a config change. The wedge is their existing subscription.
The moat is governance.

## Target Accounts

**Tier 1: Universities with existing AI site licenses**
- Ask the account team: "Does this institution have an OpenAI or Google
  AI agreement?" If yes, this is a Tier 1 target.
- The CIO cares about governance. The researchers care about not
  changing their workflow. This solution addresses both.

**Tier 2: Universities evaluating Quick Suite**
- Institutions already in the Quick Suite pipeline but stalled because
  "the built-in AI isn't strong enough" or "we already have ChatGPT."
- The model router removes the objection.

**Tier 3: Research computing shops running HPC**
- These teams already trust AWS for compute. Quick Suite + model router
  extends that trust to AI workflows. Natural expansion from the
  research computing relationship.

## Discovery Questions

Use these in the first meeting to qualify the opportunity:

1. **"What AI tools are your researchers using today?"**
   Listen for: ChatGPT, Gemini, Copilot, "we have a site license"

2. **"How do you track AI usage and spending across departments?"**
   Listen for: "We don't" / "It's hard" / "Each department has its own"

3. **"What's your policy on AI use with sensitive research data?"**
   Listen for: compliance concerns, IRB requirements, data residency

4. **"If you could keep your current AI subscription but add
   centralized governance, would that be valuable?"**
   This is the setup question. The answer is almost always yes.

5. **"Who manages the AI vendor relationships — IT, procurement,
   or individual departments?"**
   Identifies the decision maker and the pain of decentralized spend.

## The Demo Flow

### Setup (before the meeting)
- Deploy the CDK stack in a demo account
- Configure at least two providers (Bedrock + one of OpenAI/Gemini)
- Have the CloudWatch dashboard open

### Demo Script (15 minutes)

**Minute 0-3: The problem**
"Quick Suite ships with a built-in LLM. It's fine for basic tasks.
But your researchers need Claude for deep analysis, GPT for certain
generation tasks, and you need to know who's using what and how much
it costs. Today that's impossible — everyone's using their own ChatGPT
window with no governance."

**Minute 3-8: The solution**
Open Quick Suite. Show a chat agent with the model router connected.

- Ask an analysis question → show it routed to Claude on Bedrock
- Ask a summarization question → show it routed to Nova (fast, cheap)
- Force `"provider": "openai"` → show it hitting their existing OpenAI key
- Show the response metadata: provider, model, token count, latency

"Every one of those calls — whether it went to Bedrock, OpenAI, or
Gemini — passed through the same Bedrock Guardrails. Same content
filtering. Same audit trail. Same CloudWatch metrics."

**Minute 8-12: The governance**
Switch to CloudWatch dashboard.

- Show invocations by provider (pie chart)
- Show token usage over time (who's spending what)
- Show guardrail blocks (content policy working)
- Show the CloudTrail log entry for a specific call

"This is the dashboard your CIO shows the board. This is the
compliance artifact your IRB reviewer asks for. And none of this
existed when people were just using ChatGPT in a browser."

**Minute 12-15: The path forward**
"Deploying this is one CDK command. You give us your OpenAI API key,
we put it in Secrets Manager, and tomorrow your researchers are
using GPT-4o inside Quick Suite with full governance.

Then, when you're ready, you try Bedrock models alongside OpenAI.
The routing config is a YAML file — put Claude first for analysis,
keep GPT for generation, use Nova for cheap summarization. A/B test
providers with zero code changes.

And when Bedrock wins on quality and cost — which it will for most
workloads — switching is one line in a config file."

## Internal Positioning

### For SAs
"This is a reference architecture, not a product. You deploy it in
the customer's account with CDK. It's Lambda, API Gateway, Secrets
Manager, DynamoDB — all things you already know. The novel part is
the routing logic and the governance wrapper around external providers."

### For account managers
"Position this as 'bring your own model to Quick Suite.' The customer
keeps their OpenAI or Gemini spend — we're not asking them to rip
and replace. We're adding governance and a unified workspace. Once
they're running orchestration on AWS, migration to Bedrock happens
naturally because it's cheaper and faster."

### For leadership
"This is a land-and-expand play. The existing AI subscription is the
entry point. Quick Suite is the workspace. AgentCore is the integration
layer. Bedrock is the eventual destination. The model router makes
each step frictionless."

## Competitive Positioning

| Competitor | Their Pitch | Our Counter |
|------------|-------------|-------------|
| Microsoft Copilot/Fabric | "AI built into Office 365" | "We work with your Office data too, AND you're not locked to one model. Plus: Bedrock Guardrails > Copilot safety. Show them the guardrail dashboard." |
| Google Gemini for Workspace | "AI built into Google apps" | "Keep your Gemini key. Route it through Quick Suite. You get the same model with AWS governance on top." |
| OpenAI ChatGPT Enterprise | "All-in-one AI workspace" | "No BI, no automation, no enterprise data integration, no audit trail. Quick Suite does all of that and lets you keep GPT." |

## Metrics to Track

- **Accounts where existing AI subscription was the entry point** — this
  validates the wedge strategy
- **Time from first demo to deployment** — should be <2 weeks (it's one
  CDK command)
- **Provider migration rate** — what % of calls shift from external
  providers to Bedrock over time
- **Quick Suite seats sold** — the model router is the enabler, Quick Suite
  seats are the revenue
