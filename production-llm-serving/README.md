# Production LLM Serving

This blueprint helps you size and deploy a production LLM serving architecture on Databricks. The goal is to serve your workload reliably at the right cost, using two levers: **Provisioned Throughput (PT)** and **Pay-Per-Token (PPT)**.

---

## The Two Levers

### Pay-Per-Token (PPT)
Shared infrastructure — you pay only for tokens consumed, with no capacity to manage. The tradeoff is separate hard ceilings on input and output throughput.

| | GPT OSS 120B |
|---|---|
| Input cost | $0.15 / 1M tokens |
| Output cost | $0.60 / 1M tokens |
| Max input throughput | 200,000 tokens/min |
| Max output throughput | 10,000 tokens/min |
| Max QPM | ~200 QPS (workspace limit — confirm with Databricks engineering) |

Note the 20:1 ratio between input and output limits — this reflects the underlying compute asymmetry between prefill and decode, and is why "tokens per second" as a single number is a misleading capacity metric (see below).

In practice, **QPM is rarely the binding constraint**. The output token ceiling already caps effective QPM so low (see table below) that a workspace QPM limit would only become relevant for workloads with very short outputs — classification, routing, yes/no answers — where OTPM isn't the bottleneck but request frequency is high.

### Provisioned Throughput (PT)
Dedicated inference capacity measured in **model units**, billed hourly regardless of usage. You get a guaranteed throughput floor and a burst scaling mechanism that can temporarily step up to the next model unit increment (e.g., 50 → 100 units) when capacity is available.

---

## Why "Tokens Per Second" Is the Wrong Metric

A single TPS number collapses two fundamentally different constraints — input throughput and output throughput — into one, hiding which one is actually binding for your workload.

**Prefill (input):** All input tokens are processed in a single parallel forward pass. Expensive due to O(n²) attention, but done in one step — the system can absorb a large volume of input tokens quickly.

**Decode (output):** Each output token is generated one at a time, autoregressively. Every new token must attend over the full KV cache — all input tokens plus all previously generated output tokens. This is sequential, compounds as generation length grows, and is far more resource-intensive per token than prefill.

The PPT limits make this concrete: 200,000 input tokens/min vs 10,000 output tokens/min — a 20:1 ratio. **Output throughput is almost always the binding constraint.** A workload generating long responses will hit the output ceiling long before it approaches the input ceiling.

Two workloads with identical total tokens behave very differently. Working through the math against the GPT OSS 120B PPT limits:

| Workload | Input | Output | ITPM ceiling | OTPM ceiling | Binding constraint | Effective QPM |
|---|---|---|---|---|---|---|
| RAG / Q&A | 2,000 | 200 | 200,000 ÷ 2,000 = 100 QPM | 10,000 ÷ 200 = 50 QPM | **OTPM** | 50 QPM |
| Code / summarization | 200 | 2,000 | 200,000 ÷ 200 = 1,000 QPM | 10,000 ÷ 2,000 = 5 QPM | **OTPM** | 5 QPM |

OTPM is the binding constraint for both — even for RAG with its short outputs. At 50 QPM for RAG and 5 QPM for code summarization, most production workloads will outgrow PPT quickly.

**Size against your output token ceiling first. It will bind before anything else.**

---

## How to Size for Production

### 1. Is PPT Enough?

**Step 1 — Profile per-request token shape using `profile_workload.ipynb`**

The notebook measures per-request metrics against a PPT endpoint:
- Average input tokens per request
- Average output tokens per request
- TTFT and TPOT (latency characteristics)

Do not guess — real distributions are rarely what you expect, and input/output ratios vary significantly by use case.

**Step 2 — Determine whether PPT can serve your load**

There are two paths depending on whether you have historical traffic data.

*Path A — You have production QPM data*

Combine your measured token shape with your observed QPM to derive ITPM and OTPM, then compare against the PPT ceilings:

```
ITPM = avg_input_tokens_per_request  × QPM  →  compare against PPT ITPM ceiling
OTPM = avg_output_tokens_per_request × QPM  →  compare against PPT OTPM ceiling
```

*Path B — No historical data (new workload)*

Reverse-calculate the maximum QPM PPT can serve for your measured request shape. `profile_workload.ipynb` does this automatically after the profiling run:

```
max_qpm_from_itpm = PPT_ITPM_ceiling / avg_input_tokens_per_request
max_qpm_from_otpm = PPT_OTPM_ceiling / avg_output_tokens_per_request
ppt_max_qpm       = min(max_qpm_from_itpm, max_qpm_from_otpm)  ← binding constraint
```

This is your PPT capacity ceiling for your specific workload shape. If your expected QPM will exceed it, PT is required.

**Decision:**

- **QPM below `ppt_max_qpm`:** Stay on PPT. No PT needed. Done.
- **QPM at or above `ppt_max_qpm`:** PT is required — not as a cost optimization, but because PPT physically cannot serve your load.

### 2. Size Your PT Endpoint

`profile_workload.ipynb` already measured your average input and output tokens per request — those go directly into the pricing calculator. The only remaining input is your **QPM floor**.

#### Determine your QPM floor

**Existing workload** — QPM is already in your observability stack (Datadog, Grafana, CloudWatch, Databricks system tables). Pull the distribution over a representative window (1–2 weeks captures daily and weekly rhythm) and use P75–P90.

**Net new workload** — estimate from business inputs:

| App type | QPM estimate |
|---|---|
| Chatbot / assistant | `DAU × sessions_per_day × messages_per_session ÷ active_minutes_per_day` |
| API feature (autocomplete, classification) | `app_requests_per_min × LLM_calls_per_request` |
| Batch enrichment | `records_per_batch ÷ SLA_window_in_minutes` |
| RAG pipeline | `user_query_rate_per_min × LLM_calls_per_query` |

These formulas give an average QPM. Apply a 2–3× safety factor for burst and ramp-up.

#### Use the pricing calculator

Use the **[Databricks GenAI Pricing Calculator](https://www.databricks.com/product/pricing/genai-pricing-calculator)** to get a model unit recommendation. It takes:

- Cloud provider + region
- Model
- **Average input tokens** per request — from `profile_workload.ipynb`
- **Average output tokens** per request — from `profile_workload.ipynb`
- **QPM floor** — your P75–P90 from above, not your average and not your peak

Burst scaling and PPT fallback handle everything above your provisioned floor.

#### Validate empirically

1. Deploy at the recommended model unit level
2. Load test with traffic that matches your real input/output distribution
3. Observe where you start seeing 429s — that is your actual capacity ceiling
4. Adjust model units and repeat if needed

### 3. Serve Requests: PT with PPT Fallback

PT provisioned capacity guarantees your QPM floor, but traffic is never flat. `pt_ppt_router.ipynb` implements a three-layer stack that handles overflow without requiring you to overprovision PT for every possible spike.

```
┌─────────────────────────────────────────────────────┐
│  PPT fallback          ← QPM spikes PT burst         │
│                           cannot absorb              │
├─────────────────────────────────────────────────────┤
│  PT burst (100 units)  ← moderate QPM spikes above   │
│                           your provisioned floor     │
├─────────────────────────────────────────────────────┤
│  PT provisioned (50 units) ← your QPM floor          │
│                              (P75–P90, guaranteed)   │
└─────────────────────────────────────────────────────┘
```

**PT provisioned** — your guaranteed floor, always available. Size this at P75–P90 QPM so routine traffic never touches the other layers.

**PT burst** — when traffic exceeds the provisioned floor, Databricks automatically steps up one model unit increment (e.g., 50 → 100 units) if regional capacity is available. This happens without any configuration change, but it is best-effort: when burst capacity is unavailable, the endpoint returns 429.

**PPT fallback** — when PT returns a 429 or 503, `pt_ppt_router.ipynb` retries the request immediately against the equivalent pay-per-token model. PPT is elastic shared infrastructure: it has throughput ceilings (calculated in Section 1) but no provisioned capacity to exhaust, making it the right layer to absorb spikes that PT burst cannot.

#### How `pt_ppt_router.ipynb` implements the fallback

Every call goes through the `PTWithPPTFallback.predict()` method:

1. The request is sent to the PT endpoint
2. On a 429 (capacity exceeded) or 503 (service unavailable), the client retries immediately against the PPT model — same messages, same parameters, same return type
3. The routing is transparent to the caller; streaming and non-streaming both follow the same path

The status codes that trigger fallback are configurable in `config.yml`:

```yaml
model_serving:
  fallback_status_codes:
    - 429  # capacity exceeded
    - 503  # service unavailable
```

#### When PPT fallback is and isn't sufficient

The fallback is designed to absorb short, unpredictable bursts — not sustained overload. A request routed to PPT is subject to PPT's throughput ceilings. If a spike is large enough or lasts long enough, PPT will also saturate — its ceiling for your specific workload shape is the `ppt_max_qpm` that `profile_workload.ipynb` calculated.

If your fallback rate is consistently elevated, that is a signal to increase PT model units, not to rely on PPT as a permanent second tier.

---

## Contents

| File | Purpose |
|---|---|
| `config.yml` | All configurable parameters — endpoint names and serving settings |
| `profile_workload.ipynb` | Profile your workload on PPT: measure input/output tokens, TTFT, TPOT |
| `pt_ppt_router.ipynb` | MLflow pyfunc model: PT endpoint with PPT fallback routing |
