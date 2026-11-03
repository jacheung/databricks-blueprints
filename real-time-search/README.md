# Real-time search: QAC and SERP with session-aware reranking

**Question:** Does a session-aware L2 reranker improve query autocomplete (QAC) and search results (SERP) relevance
enough to justify its latency, and can Databricks serve both within real-time budgets (QAC < 40 ms, SERP < 400 ms,
feature freshness < 1 s)?

**Takeaway:** pending. Measured so far: session features reach the online store from Kafka in **p50 58 ms /
p95 442 ms** (18 single-event probes), against p50 158 s when they streamed from a Delta table.

Retrieve-then-rank on both surfaces: L1 retrieves candidates (QAC: Lakebase prefix lookup; SERP: AI Search hybrid),
an optional L2 reranks them with clickstream features from Feature Views, and the app falls back to the L1 order
when L2 is slow. Data is Amazon ESCI plus simulated sessions with a planted intent. Build status is in
[PLAN.md](PLAN.md); deferred scope is in [PARKING_LOT.md](PARKING_LOT.md).

## Repo layout and team ownership

The folders follow who owns what in production. Each one except `shared/` is its own Declarative Automation Bundle,
and the bundles only meet through the contracts in `shared/`.

```
shared/   contracts + platform helpers, synced into every bundle
  configs/              example.yml (committed), <target>.yml (gitignored, e.g. dev.yml)
  src/rts_shared/       config.py, events.py (contract), features.py (contract), kafka.py
data/     bundle rts-data (data engineering): catalog, clickstream, Kafka Stream, Feature Views
  resources/            catalog_job, clickstream_job, teardown_job
  notebooks/  src/rts_data/  tests/
ml/       bundle rts-ml (search relevance): L1 retrieval, L2 rankers, evaluation, benchmarks
  src/rts_ml/  tests/   jobs are added with their build steps
app/      bundle rts-app (frontend): FastAPI + React, added in build step 4
```

| Contract | Owner | Consumers | Enforced by |
|---|---|---|---|
| Event schema and topic (`rts_shared.events`, `kafka.topic`) | data | app (produces), ml (trains) | `session_events` DDL and the Kafka payload schema are both generated from `EVENT_FIELDS` |
| Feature catalog (`rts_shared.features`: names and lookup keys) | data | ml | `data/tests/test_feature_specs.py` asserts the feature specs match it exactly |
| Retrieval outputs (prefix table, index columns) | ml | app | added in build step 3 |
| L2 endpoint I/O, timeout and fallback | ml | app | added in build steps 4–5 |

Bundles refer to each other's objects by name through the shared config, never through `${resources...}`, so each
can deploy on its own (and later under its own service principal).

## How to run

1. Copy `shared/configs/example.yml` to `shared/configs/dev.yml` and fill it in. Notebooks run interactively read
   `dev.yml`; jobs read `shared/configs/<bundle target>.yml`.
2. Run the tests from this folder: `uv run --no-project --with pytest --with pyyaml python -m pytest -q`.
3. Deploy and run the data bundle:
   ```bash
   cd data
   databricks bundle deploy -t dev -p <profile>
   databricks bundle run catalog_job -t dev -p <profile>       # preflight, then ESCI + ai_classify
   databricks bundle run clickstream_job -t dev -p <profile>   # sim -> Kafka Stream -> Feature Views + checks
   ```
   `clickstream_job` skips the simulator when `session_events` exists. To rebuild the data, run `teardown_job` with
   `--params confirm=yes` first, then `clickstream_job` with `--params rebuild_data=true`.

Every notebook ends with checks that assert, so a green run means its claim held.

## Cleanup

- `databricks bundle run teardown_job -t dev -p <profile> --params confirm=yes` deletes the Feature Views and the
  Kafka Stream (with `confirm=no` it only lists them). The session streaming pipeline goes with them.
- `databricks bundle destroy -t dev -p <profile>` in each bundle folder removes the jobs and uploaded files.
- Tables in `<catalog>.<schema>`, the online store (Lakebase), the UC Kafka connection and the topic are kept.
  Drop them by hand if you're done with them.
