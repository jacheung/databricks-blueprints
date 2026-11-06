# Production LLM Serving

Sizing and comparing production model serving options on Databricks.

| Workflow | Use it to | Start here |
| --- | --- | --- |
| [Serving bench](serving-bench/) | Find which serving methods (provisioned throughput, pay-per-token, custom GPU) can serve a given open-source model, measure your request shape from sample prompts or traces, deploy the options you choose, and benchmark throughput, latency, and latency after idle at that shape. Works by hand or with a coding agent. | Run discovery for your model, optionally profile your requests, then the deploy and benchmark notebooks; or start a coding agent in the folder and say "benchmark \<model\>" ([AGENTS.md](serving-bench/AGENTS.md)). |

See the [serving bench README](serving-bench/README.md) for configuration, execution, and serving the winner behind Unity Gateway with pay-per-token fallback.
