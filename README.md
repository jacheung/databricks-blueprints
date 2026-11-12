# 🧱🔷 Databricks Blueprints
This is a collection of various demos that leverages the ever-growing feature set of Databricks Data Intelligence Platform.

Blueprints are grouped by the question they answer:

- [Solutions](solutions/): *How do I build X on Databricks?* Complete reference builds.
- [Benchmarks](benchmarks/): *Which option should I pick, and does it hold up?* Repeatable measurements with a readout.
- [Patterns](patterns/): *How do I wire up this one feature?* Small, focused how-tos.

Unfinished work lives in its kind's folder and is marked **In progress** below.

| Blueprint | Kind | Status | Focus |
| --- | --- | --- | --- |
| [real-time-search](solutions/real-time-search/) | Solution | In progress | Personalized e-commerce query auto-completion and search results ranking (L1 retrieval, L2 rerank). |
| [recommender-system-end-to-end](solutions/recommender-system-end-to-end/) | Solution | Complete | Cart-checkout recommendations: collaborative filtering for logged-in users, market basket analysis for guests. |
| [rag-blueprints](solutions/rag-blueprints/) | Solution | Complete | Two parameterized RAG chains: Databricks Vector Search and open-source FAISS. |
| [scaling-forecasting](solutions/scaling-forecasting/) | Solution | In progress | Forecasting with Prophet and composite models. |
| [OTEL-fine-tuning](solutions/OTEL-fine-tuning/) | Solution | In progress | Turning OpenTelemetry traces into ChatML training data and fine-tuning with TRL, Ray Train, or Mosaic AI. |
| [llm-serving-bench](benchmarks/llm-serving-bench/) | Benchmark | Complete | Which Databricks serving method meets a Hugging Face model's throughput and latency needs. |
| [service-policy-benchmark](benchmarks/service-policy-benchmark/) | Benchmark | Complete | Unity Gateway service-policy accuracy, and client-side `ai_decide` versus service policies. |
| [multimodal-lance](benchmarks/multimodal-lance/) | Benchmark | In progress | Lance for multimodal image training, benchmarked against Delta storage patterns. |
| [ai-gateway-auth-patterns](patterns/ai-gateway-auth-patterns/) | Pattern | Complete | Service principal and on-behalf-of authentication for AI Gateway. |
| [real-time-endpoint-tracing](patterns/real-time-endpoint-tracing/) | Pattern | Complete | MLflow tracing for asynchronous calls and model serving. |
| [quick-end-to-end-ML](patterns/quick-end-to-end-ML/) | Pattern | Complete | Synthetic data, model training, champion/challenger validation, and deployment. |
| [split-source-table-per-index](patterns/split-source-table-per-index/) | Pattern | Complete | One narrow, CDF-enabled table per AI Search index, so each index only syncs changes to its own columns. |

## What every runnable blueprint should document

A blueprint can be a solution, a benchmark, or a pattern. Structure each runnable workflow's README around three sections:

1. **Question** — State the question we are answering in one simple sentence.
2. **Takeaway** — Explain what the results conclude, including the conditions and limitations that matter. If results are not available yet, say so.
3. **How to run** — Document prerequisites (compute, permissions, and data), configuration changes, run order, expected output, and cleanup.
