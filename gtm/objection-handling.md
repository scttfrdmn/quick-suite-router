# Objection Handling

Every objection you'll hear, and how to turn it into a conversation.

---

## "We already have an OpenAI subscription."

**Do not** say: "Bedrock is better."
**Do** say: "Perfect. Let's put it to work inside Quick Suite."

**The full response:**
"That's actually ideal. The model router lets you bring your existing
OpenAI key into Quick Suite — your researchers keep using GPT, your
spend doesn't change, and you get governance, audit trails, and cost
visibility that you don't have today. It's a 15-minute deployment.
Can I show you what that looks like?"

**Why this works:** You've agreed with the customer, validated their
existing investment, and offered to make it better without asking them
to change anything. The conversation continues instead of ending.

**The follow-up (once deployed):**
"Now that you're running GPT through the router, would you like to
try Claude on Bedrock alongside it for analysis tasks? It's one line
in the config. You can compare quality and cost with real workloads,
not benchmarks."

---

## "We already have Google Gemini through Workspace."

**Response:**
"Great — the model router integrates with Gemini directly. Your
researchers keep using the model they know, and you get AWS-native
governance on top: Bedrock Guardrails, CloudWatch metrics, CloudTrail
audit logs. Your Google Workspace relationship becomes the on-ramp
to Quick Suite, not a competitor to it."

---

## "The built-in Quick Suite LLM is fine for us."

**Response:**
"It is fine for many tasks. But when your researchers need to analyze
a 200-page grant proposal, or your finance team needs to generate a
complex budget model, or someone needs code assistance — the model
router lets Quick Suite reach for Claude, GPT-4o, or Gemini for those
heavy tasks while keeping the built-in model for everyday queries.
It's not replacing anything; it's adding capability."

---

## "We don't want to manage another piece of infrastructure."

**Response:**
"It's fully serverless — Lambda, API Gateway, DynamoDB. There's
nothing to patch, no servers to manage, no scaling to configure.
It deploys with one CDK command and costs less than $5/month in
infrastructure. The only ongoing task is rotating API keys in
Secrets Manager, which your team probably already does."

---

## "We're concerned about sending data to external AI providers."

**Response:**
"That's exactly why you want the router. Today, your researchers are
sending data to OpenAI and Gemini directly — from their browsers,
with no governance, no audit trail, and no content filtering. The
model router puts Bedrock Guardrails in front of every call, blocks
PII before it leaves your environment, and logs everything in
CloudTrail. You're not adding external providers — you're governing
the ones you already use."

---

## "Can't we just use Bedrock directly?"

**Response:**
"Absolutely, and Bedrock is the default provider. But many institutions
have existing AI subscriptions with contractual commitments. The router
lets them honor those commitments while running everything through AWS
governance. Over time, as they see Bedrock's quality and cost
advantages, they naturally migrate. The router makes that migration
frictionless — it's a YAML config change, not a re-architecture."

---

## "This seems like it could be expensive at scale."

**Response:**
"The infrastructure cost is negligible — under $5/month for Lambda,
API Gateway, and DynamoDB. The LLM costs are what they'd be anyway;
you're just routing them through governance now. And the response
cache can reduce redundant calls by 20-40% depending on workload,
which actually saves money.

Here's what's really expensive: 500 researchers each with their own
ChatGPT Pro subscription at $200/month, with no visibility into
what they're doing and no governance. That's $100K/month with zero
audit trail. The model router gives you one bill, one dashboard,
one policy."

---

## "We need to evaluate this with a pilot group first."

**Response:**
"That's the ideal approach. Deploy the stack, configure one provider
(even just Bedrock with no external keys), connect it to Quick Suite,
and give it to a small group. The whole setup is 15 minutes. The
pilot will tell you three things: which models your users prefer,
how much it actually costs, and whether the governance satisfies
your compliance requirements. All of that shows up in the CloudWatch
dashboard automatically."

---

## "What happens if one of the providers has an outage?"

**Response:**
"The router has automatic fallback built in. Each task type has a
preference list — if the primary provider returns an error or times
out, it automatically tries the next one. For example, if OpenAI is
rate-limited, the router falls back to Claude on Bedrock. Your users
see a response; they don't see the provider switch. And the fallback
is logged in CloudWatch so you know it happened."

---

## "Our security team will need to review this."

**Response:**
"Of course. Here's what they'll find:

- All secrets in AWS Secrets Manager (KMS-encrypted)
- IAM least-privilege policies on every Lambda
- OAuth 2.0 authentication via Cognito
- All API calls logged in CloudTrail
- No data stored at rest except the optional response cache (DynamoDB
  with TTL-based expiration, encrypted)
- No credentials in code or environment variables
- The CDK stack is open source — they can audit every line

We can walk through the architecture with your security team whenever
they're ready."

---

## "Why wouldn't we just build this ourselves?"

**Response:**
"You absolutely could. It's Lambda functions and API Gateway — there's
no magic. But the value isn't in the code, it's in the operational
readiness: the routing logic, the fallback chains, the governance
wrappers, the CloudWatch dashboard, the Guardrails integration, and
the Quick Suite MCP connection. Building and testing all of that takes
weeks. Deploying this takes 15 minutes. And it's open source — you
own it, you can modify it, and you're not dependent on anyone."

---

## Conversation Starters

When an AM hears any of these, they should engage:

| You hear... | You say... |
|-------------|-----------|
| "We have ChatGPT Enterprise" | "Have you thought about governing those calls centrally?" |
| "Our researchers all use Gemini" | "Would it help to have one dashboard showing AI usage across the institution?" |
| "We're worried about AI compliance" | "What if every AI call — regardless of provider — went through the same content policy?" |
| "Quick Suite's AI isn't strong enough" | "What if Quick Suite could use Claude, GPT-4o, and Gemini?" |
| "We can't justify another AI subscription" | "What if you could use the one you already have, but better?" |
