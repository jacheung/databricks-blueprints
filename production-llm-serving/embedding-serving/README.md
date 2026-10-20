# Choosing an embedding serving method

**Choose the lowest-cost serving option that meets your required RPS and latency at expected concurrency.** This benchmark compares provisioned throughput (PT), Express GPU serving on T4/A10G, and pay-per-token (PPT) for GTE-Large-EN-v1.5.

## Example results

October 20, 2026 run, using 256-token inputs:

| Serving option | Peak RPS | Reported saturation concurrency | P50 at saturation | Billing |
| --- | ---: | --- | ---: | --- |
| PT | ~127 | 16 | 111 ms | Provisioned capacity |
| Express GPU — A10G | ~34 | 4 | 118 ms | Always-on GPU capacity |
| Express GPU — T4 | ~12 | 2 | 161 ms | Always-on GPU capacity |
| PPT | ~27 | Variable; shown at 16 | 72 ms | Per token; no idle usage charge |

Typical latency was similar within each option's capacity. Beyond saturation, more concurrent requests mainly increased queuing latency rather than throughput. PPT maintained low median latency but showed larger tail latency in this run.

PT delivered the highest throughput. A10G provided an intermediate capacity option; T4 supported the lowest request rate. PPT avoids idle capacity charges, making it worth evaluating for intermittent traffic. Compare actual pricing and utilization before choosing: this package does not calculate monetary costs.

## Run

1. Keep both notebooks and [config.yml](config.yml) together in a Databricks workspace folder. Update catalog, schema, endpoint names, and test settings.
2. Run [01 Endpoint Setup and Deployment](<01 Endpoint Setup and Deployment.py>) on serverless GPU with **Databricks AI environment v5**, which bundles Transformers 4.57.1. It registers GTE and creates the PT and Express GPU endpoints.
3. Run [02 Embedding Endpoint Benchmarking](<02 Embedding Endpoint Benchmarking.py>) to measure latency, achieved RPS, errors, and latency after idle.
4. Use the setup notebook's opt-in cleanup cell to remove dedicated test endpoints when finished; they incur charges while running.

## Scope

The run uses 100 requests per concurrency level. P50 summarizes typical latency; P95/P99 are exploratory tail estimates. Idle tests use three probes per duration, up to five minutes. They measure latency after idle without isolating cold starts from shared-service load, networking, or connection setup. Results apply to the tested configurations and workload.
