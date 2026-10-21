---
name: serving-bench
description: Benchmark an open-source Hugging Face model (embedding or chat) across Databricks serving methods — provisioned throughput, pay-per-token, and custom GPU (Express) — and produce an RPS × concurrency and latency × idle-time readout. Use when someone asks which serving option to use for a specific model, or wants to compare PT, PPT, and GPU serving for it.
---

# Serving bench

Guide the customer from "I want to serve model X" to a measured comparison table. You do the
orchestration; the customer approves choices. Files in this folder:

| File | Role |
| --- | --- |
| `discover.py` | Read-only: which methods can serve the model, with reasons and ready-to-paste endpoint blocks |
| `configs/<model>.yml` | The plan you write after approval (shape: `configs/example.yml`); gitignored |
| `01_deploy.py` | Notebook: logs/registers custom model, creates dedicated endpoints, waits, smoke-tests |
| `02_benchmark.py` | Notebook: concurrency sweep + idle test → UC tables; exits with the readout JSON |
| `00_discover.py` | Notebook wrapper around `discover.py` for in-workspace agents |
| `make_readout.py` | Builds `readouts/<model>/` (markdown + charts) from a run's UC data |
| `00_compat_check.py` | Notebook: loads the model and runs one inference on a given AI env; exits `{ok, ...}` JSON |

Never auto-select a Databricks profile — list `databricks auth profiles` and let the customer choose.
Pass `--profile <P>` on every CLI call.

## 1. Gather inputs

Ask for: HF repo id (e.g. `Alibaba-NLP/gte-large-en-v1.5`), task (`embedding` or `chat`), profile,
and a UC `catalog.schema` they can write to (custom models and results go there). Gated HF models need
`HF_TOKEN` set locally for discovery.

## 2. Discover

Local: `python discover.py --model <HF> --task <task> --profile <P> --json` (needs `databricks-sdk`).
In workspace: run `00_discover` with the `hf_name`/`task` widgets.

Output: `model` (settings for the config) and `options`, each with `method`, `target`, `viable`,
`reason`, `endpoint`. Show the customer a table of all options, including the non-viable ones and why.
Explain:

- **pt** — dedicated capacity on the `system.ai` copy of the model. `endpoint` is set to the smallest
  increment. Higher increments are multiples of it. Billed while provisioned.
- **ppt** — shared endpoint, billed per token, no idle cost; tail latency can vary with shared load.
  Identity comes from the catalog's HF link; the exact upstream revision isn't exposed.
- **custom** — the HF model on a dedicated GPU through Express deployment. GPU fit is a memory
  estimate, not a guarantee; the deploy step proves it. Only single-GPU shapes; GPU availability
  varies by region.

`model.ai_env` is the newest numbered serverless GPU AI environment this workspace offers (read from
its base environments, the same way in a notebook or from the CLI), and `model.ai_env_candidates`
the newest three. Custom models are logged on `ai_env`; whether the model's code works with that
env's libraries is only known by trying (step 4).

## 3. Get approval, write the config

Ask which options to benchmark and confirm parameters: PT capacity (default: one increment), GPU
shapes, scale-to-zero on/off for custom (off gives stable throughput numbers; on lets the idle test
show cold starts). Mention that dedicated endpoints bill until deleted, and that `02_benchmark`
deletes the ones it created when it finishes (`cleanup.delete_endpoints_after_benchmark`).

Write `configs/<hf repo name, lowercased>.yml` (e.g. `configs/gte-large-en-v1.5.yml`; the same name
as its readout). If it already exists for this model, reuse it and edit only what changed. Contents: `model` block from discovery output plus `task`; `unity_catalog`; the approved
`endpoint` blocks; `load_test`/`idle_test` from `configs/example.yml` unless the customer wants changes
(smaller `requests_per_level` and `idle_test.idle_seconds: []` for a quick pass). Show it before deploying.

## 4. Deploy

Upload the folder (skip `tests/`):

```bash
DIR=/Workspace/Users/<me>/serving-bench
databricks workspace import-dir . $DIR --overwrite --profile <P>
```

Submit `01_deploy`. If any custom endpoint is selected it must run on Serverless GPU with the AI env
from `model.ai_env` (`ENV=<model.ai_env>`); for PT/PPT-only configs drop `compute`/`environments`.

```bash
databricks jobs submit --no-wait --profile <P> --json '{
  "run_name": "serving-bench deploy",
  "timeout_seconds": 7200,
  "tasks": [{
    "task_key": "deploy",
    "notebook_task": {"notebook_path": "'$DIR'/01_deploy", "base_parameters": {"config_path": "'$DIR'/configs/<model>.yml"}},
    "environment_key": "gpu",
    "compute": {"hardware_accelerator": "GPU_1xA10"}
  }],
  "environments": [{"environment_key": "gpu", "spec": {"base_environment": "workspace-base-environments/databricks_ai_v'$ENV'"}}]
}'
```

Poll `databricks jobs get-run <run_id> --profile <P>` every few minutes; PT and GPU endpoints commonly
take 15–30 min. In parallel, `databricks serving-endpoints get <name>` shows `state.ready` /
`state.config_update`. On failure, read the task output (`databricks jobs get-run-output <task_run_id>`)
and the endpoint build logs (`databricks serving-endpoints build-logs <name> served`). Report the cause.
Don't silently swap GPU, AI env, or model.

In workspace: run `01_deploy` interactively on Serverless GPU (AI env `model.ai_env`) instead.

### If the model check fails

`01_deploy` loads the model and runs one inference before creating anything. If that fails, the run
fails with a `ModelCheckError` naming the AI env, transformers/torch versions, and the error. Nothing
was registered or created, so there is nothing to clean up. It is usually a library-version mismatch
(e.g. remote code written for transformers 4.x on an env with 5.x).

1. Show the customer the error and **ask** whether to run a compatibility check on the other
   candidates in `model.ai_env_candidates` (the next two older envs). Each is a short A10 job.
2. If yes, submit `00_compat_check` once per remaining candidate, in parallel, each on its own base
   environment, with `base_parameters` `{"config_path": ..., "ai_env": "<v>"}` (same JSON as deploy;
   change `notebook_path`, `run_name`, the env number, and use `"timeout_seconds": 3600`).
3. When all finish, read each `databricks jobs get-run-output <task_run_id>` → `notebook_output.result`
   (`{ok, ai_env, transformers, torch, error}`). Show the customer the pass/fail table.
4. If any passed: set `model.ai_env` to the **newest passing** version in the config, re-upload it,
   and resubmit `01_deploy` on that env without asking again. Tell the customer which env you used.
5. If none passed: stop and report every error. Don't try older envs, pin libraries, or change the
   model without the customer's direction.

## 5. Benchmark

Submit `02_benchmark` on serverless CPU (no `compute`/`environments`), same `config_path`. A full run
is roughly endpoints × levels × requests plus `sum(idle_seconds) × reps` (~25 min idle for the default).

```bash
databricks jobs submit --no-wait --profile <P> --json '{
  "run_name": "serving-bench benchmark",
  "timeout_seconds": 10800,
  "tasks": [{"task_key": "benchmark",
             "notebook_task": {"notebook_path": "'$DIR'/02_benchmark", "base_parameters": {"config_path": "'$DIR'/configs/<model>.yml"}}}]
}'
```

When it finishes, `databricks jobs get-run-output <task_run_id>` → `notebook_output.result` is JSON
with `run_id`, `readout`, `readout_markdown`, `readout_document`, and `deleted_endpoints`. Raw data for
follow-up questions:

```bash
databricks experimental aitools tools query --profile <P> \
  "SELECT endpoint, concurrency, rps, p50, p95, p99, errors FROM <catalog>.<schema>.serving_bench_summary WHERE run_id = '<run_id>' ORDER BY endpoint, concurrency"
```

`serving_bench_requests` has every request (`phase` = warmup | load | idle_warmup | idle_probe).

## 6. Report

Build the readout folder from the run's saved data (no endpoints needed):

```bash
python make_readout.py --config configs/<model>.yml --run-id <run_id> --profile <P>
```

It writes `readouts/<hf repo name, lowercased>/README.md` with the summary table, settings, endpoints,
four charts (throughput, latency percentiles, latency distribution, latency after idle), and a
per-level table. One folder per model; it replaces the previous run's; history stays in the UC tables.
Needs `matplotlib` and `databricks-sdk`. In a notebook, call `fetch_run(run_id, catalog, schema,
lambda sql: [r.asDict() for r in spark.sql(sql).collect()])` and `write_readout(...)` instead.
Re-run it any time to regenerate charts from the same run.

Present `readout_markdown` plus short takeaways grounded in the numbers:
- Peak RPS and where it saturates; beyond saturation, extra concurrency adds queueing, not throughput.
- Latency at saturation vs C=1.
- Latency after the longest idle vs warm, its std deviation, and any idle failures. Don't call a
  client-side idle delta a "cold start" for PPT; shared pools have other load.
- Errors/timeouts per endpoint. Levels with >1% errors are excluded from peak/saturation and listed in
  Notes. Report a rate limit (HTTP 429) as a limit of that endpoint in this workspace, not as capacity.
- Sample sizes: a level with ≥100 requests gives a usable P95; P99 and 3-rep idle stats (including
  the std) are exploratory. Check `duration_s` in `serving_bench_summary`; a level that finished in
  well under a few seconds has a noisy RPS. Raise `requests_per_client` and rerun.

Don't compute cost. The customer picks the cheapest option that meets their RPS/latency needs, using
their own pricing.

## 7. Cleanup

Serving endpoints can't be stopped, only deleted. `02_benchmark` deletes the dedicated endpoints it
created (methods `pt` and `custom` tagged `created_by=serving-bench`) after saving results; confirm
them against `deleted_endpoints`. To measure again, rerun `01_deploy` (it re-logs the model as a new
version, then provisions endpoints). Never delete `ppt` endpoints.

If the benchmark failed before its cleanup cell, or cleanup was disabled, offer to delete them. Confirm
first:

```bash
databricks serving-endpoints delete <name> --profile <P>
```
