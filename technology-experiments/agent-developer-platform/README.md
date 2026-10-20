# GenAI EPL Reporting Boundaries

## Working model

The GenAI EPL surface has three related calls:

1. Interactive Agents
2. AI Functions
3. Core GenAI

These should be treated as distinct product motions for reporting. They can
contribute to the same customer outcome, but a single outcome should have one
primary attribution to avoid double reporting.

## Agent Serving POV

> Databricks Agent Serving is framework-native and primitive-first.
> `ResponsesAgent` is an optional managed-lifecycle contract for teams that
> want a standard agent protocol and tighter MLflow/Databricks integration.

The platform differentiation is its control plane: governed data and tools,
identity, scalable runtime, tracing, evaluation, monitoring, deployment, and
cost controls. `ResponsesAgent` is the opt-in path for teams that value a
common agent protocol and more tightly integrated lifecycle operations; it is
not a prerequisite for framework freedom or MLflow instrumentation.

The common protocol also protects downstream applications from framework
changes. A web app, Slack bot, or evaluation harness can retain the same
request, streaming, and output contract while the agent implementation moves
between LangGraph, custom orchestration, or another framework. The adapter
must still preserve the contract's semantics, including streaming events,
tool-call and tool-result formats, conversation state, errors, safety, and
latency expectations.

### First-class integrations

`ResponsesAgent` is the compatibility layer that lets Databricks product
surfaces understand an arbitrary agent implementation. It provides out-of-box
integration with:

- AI Playground and template chat UIs through an OpenAI Responses API-style
  request, response, streaming, and tool-call contract.
- Agent Evaluation and review workflows that can submit consistent inputs,
  render outputs and tool history, and connect them to traces.
- Agent Monitoring through automatic aggregation and display of streamed
  responses.
- MLflow AgentServer's standard `/responses` endpoint, including request
  routing, logging, and error handling in the Databricks Apps templates.

It does not make tracing, evaluation, Git/DAB deployment, custom routes, or
agent orchestration exclusive to Databricks. Those remain available to
framework-native applications; `ResponsesAgent` avoids bespoke integration
work when teams want first-class Databricks agent experiences.

## AI Functions and batch inference

AI Functions are the developer interface: built-in SQL and PySpark functions
such as `ai_extract`, `ai_classify`, `ai_summarize`, and `ai_query`. Batch
inference is the execution pattern of applying those functions to stored data
at scale through SQL, notebooks, Lakeflow, or Workflows.

For an `ai_query` call to a Databricks-hosted Foundation Model API in a batch
workflow:

- Product adoption: AI Functions.
- Consumption attribution: Model Serving with the `BATCH_INFERENCE` offering
  type, plus the compute that runs the query or pipeline.
- Exception: the documented task-specific functions `ai_parse_document`,
  `ai_extract`, and `ai_classify` can be recorded under the `AI_FUNCTIONS`
  product instead.

## Reporting recommendation

Core GenAI should not automatically exclude Interactive Agents or AI
Functions. Instead, report at two levels:

| Level | Rule |
| --- | --- |
| Portfolio | Include all three calls in the GenAI agenda when they advance a shared customer use case. |
| Win/loss/blocker attribution | Assign one primary product motion: Interactive Agents, AI Functions, or Core GenAI. Capture the other motions as attached dependencies or expansion paths, not separate wins. |

Examples:

- A customer deploys an Interactive Agent backed by an AI Function-enriched
  table: primary win is Interactive Agents; AI Functions is an enabling
  dependency.
- A customer operationalizes `ai_query` in a Lakeflow pipeline without an
  agent experience: primary win is AI Functions; batch-inference consumption
  is the execution and metering view, not another product win.
- A shared blocker, such as governance, model availability, or procurement,
  can be tagged across the relevant product motions. It should be counted once
  in an aggregate blocker total, with cross-product tags for routing.

## Open decision

Confirm the official GenAI EPL role charter before treating this as policy.
The linked role document requires Google authentication and was not accessible
from the sandbox, so this note reflects the working interpretation from the
Databricks product documentation and this discussion.
