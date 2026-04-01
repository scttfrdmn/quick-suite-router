# Account Manager Talking Points

## The One-Liner

"Quick Suite Model Router lets your customers bring their existing
OpenAI or Google AI subscription into Quick Suite with full AWS
governance — then naturally migrate to Bedrock over time."

## The Three Things to Remember

1. **It's additive, not competitive.** We're not asking the customer
   to drop their OpenAI contract. We're making it better by wrapping
   it in guardrails, audit trails, and cost visibility.

2. **Deployment is one command.** This isn't a six-month integration
   project. It's a CDK stack that deploys in 15 minutes. Populate
   the API key, connect to Quick Suite, done.

3. **The real sale is Quick Suite seats.** The model router is the
   enabler. It removes the "we already have AI" objection and opens
   the door to Quick Suite's full value: BI, automation, research,
   and the agentic workspace.

## When to Bring This Up

- **Customer says "we already have ChatGPT/OpenAI"** → "Perfect,
  let's bring that into Quick Suite."
- **Customer is evaluating Quick Suite but worried about the LLM
  quality** → "You can use Claude, GPT, or Gemini through Quick
  Suite, not just the built-in model."
- **CIO asks about AI governance** → "We can govern every AI call
  across providers — one dashboard, one policy, one audit trail."
- **Procurement asks about AI vendor consolidation** → "Start with
  what you have. Consolidate to Bedrock when the numbers make sense.
  The switching cost is zero."

## Talking to Different Stakeholders

### CIO / CISO
"Every AI call — whether it goes to OpenAI, Gemini, or Bedrock —
passes through the same content guardrails and shows up in the same
audit trail. You get one governance plane across all your AI providers.
SSNs are blocked, PII is anonymized, and every interaction is logged
in CloudTrail."

### VP of Research / Provost
"Your researchers keep using the models they know. Nobody's workflow
changes. But you gain visibility into who's using what, how much it
costs, and whether it complies with your research data policies."

### IT Director
"It's a CDK stack — Lambda, API Gateway, DynamoDB, Secrets Manager.
All serverless, all in your account. No new vendors, no new contracts.
You manage provider API keys in Secrets Manager. The whole thing costs
less than $5/month in infrastructure."

### Procurement
"You don't need a new AI contract. Plug in your existing OpenAI or
Google keys. When you're ready to evaluate Bedrock, it's a config
change — no migration, no new integration. You can A/B test providers
and make the switch when the economics justify it."

## Quick Suite Value Chain

```
Model Router  →  Quick Suite Seats  →  Quick Research  →  Bedrock
 (the hook)       (the revenue)       (the stickiness)   (the moat)
```

The model router gets them in the door. Quick Suite seats generate
recurring revenue. Quick Research (connecting enterprise data) creates
stickiness. And once they're running everything through AWS, Bedrock
becomes the natural default — cheaper, faster, and already integrated.

## Key Numbers

- **$20/user/month** — Quick Suite Reader Pro
- **$40/user/month** — Quick Suite Author Pro
- **~$5/month** — Model router infrastructure cost
- **$0 additional** — if using existing OpenAI/Gemini subscription
- **15 minutes** — deployment time
- **0 lines of code** — for the customer to write
