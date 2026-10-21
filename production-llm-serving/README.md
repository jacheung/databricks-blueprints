# Production LLM Serving

Two workflows for sizing and comparing production model serving options on Databricks.

| Workflow | Use it to | Start here |
| --- | --- | --- |
| [Serving bench](serving-bench/) | Find which serving methods (provisioned throughput, pay-per-token, custom GPU) can serve a given open-source model, deploy the ones you choose, and benchmark throughput, latency, and latency after idle. Works by hand or with a coding agent. | Run discovery for your model, then the deploy and benchmark notebooks; or point an agent at [SKILL.md](serving-bench/SKILL.md). |
| [LLM capacity planning](llm-capacity-planning/) | Profile token usage and latency, assess pay-per-token capacity, and size provisioned throughput with Unity Gateway fallback routing. | Follow the sizing guide and run [profile_workload.py](llm-capacity-planning/profile_workload.py). |

Each workflow contains its own guide and runnable notebooks. See its README for configuration and execution instructions.
