# Production LLM Serving

Two workflows for sizing and comparing production model serving options on Databricks.

| Workflow | Use it to | Start here |
| --- | --- | --- |
| [LLM capacity planning](llm-capacity-planning/) | Profile token usage and latency, assess pay-per-token capacity, and size provisioned throughput with Unity Gateway fallback routing. | Follow the sizing guide and run [profile_workload.py](llm-capacity-planning/profile_workload.py). |
| [Embedding serving](embedding-serving/) | Compare provisioned throughput, Express GPU, and pay-per-token against embedding throughput, latency, and cost requirements. | Configure endpoints, then run the setup and benchmark notebooks in order. |

Each workflow contains its own guide and runnable notebooks. See its README for configuration and execution instructions.
