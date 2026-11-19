# Scaling forecasting: one Prophet model per group

## Question

How do I train and forecast with thousands of per-group Prophet models (store × SKU, say) on Databricks, and when I need to keep the models, how do I store and serve them?

## Takeaway

All three paths start from the same data (`00`), and each path is one notebook you can run on its own. Pick the one that matches how you consume forecasts:

| Path | Notebook | Use when | What it creates | Infrastructure |
|---|---|---|---|---|
| **1 · No saved models** | `01_no_save_forecast` | Training and forecasting run on the same schedule (a nightly job, say) | `forecasts_<source>` table | Serverless job |
| **2 · Save → offline inference** | `02_save_offline_inference` | You forecast more often than you retrain, or want models you can audit and roll back; batch forecasts are enough | `prophet_models` feature table, `prophet_router` model, batch forecasts from `fe.score_batch` | Serverless job |
| **3 · Save → online inference** | `03_save_online_inference` | An app needs one group's forecast on demand | Path 2's table and router, plus a Lakebase online table and a serving endpoint | Serverless job, Lakebase, Model Serving (both bill while they exist) |

![Three paths from one per-group time series table. Path 1: train_and_forecast writes forecasts to a Delta table. Paths 2 and 3: train_to_json writes slim Prophet JSON to the prophet_models feature table, and the prophet_router model is logged with feature lineage to it. Path 2: fe.score_batch joins each group's model_json from the Delta table. Path 3: the feature table is published to a Lakebase online table, and the serving endpoint looks up model_json there by group_name](docs/paths.svg)

Every path fits each group with a pure-pandas function run by Spark `applyInPandas` (a grouped pandas UDF), with one partition per group, on serverless or classic compute. Why Spark and not Ray: [benchmarks/spark-vs-ray-forecasting](../../benchmarks/spark-vs-ray-forecasting/) compares them on matched cores, and they tie (within ±8% at 10k groups, uniform or skewed, with the gap set by node-to-node fit speed). Spark gets there with fewer nodes, on serverless, and without a SQL warehouse.

What we found (serverless; 10 groups × 50k hourly rows, then 100k groups × 5k rows):

- **It scales on serverless without sizing anything.** At 100k groups × 5k rows (500M rows), path 1 fit and forecast all 100k models in about 7 minutes. Training into the feature table and publishing to Lakebase took about 11 minutes. Logging the router, batch-scoring all 100k groups and bringing up the endpoint took about 21 minutes, most of it endpoint start-up. One partition per group (100k partitions) caused no problems.
- **Store slim Prophet JSON, not pickles or full JSON.** `model_to_json` embeds the whole training history and Stan's in-sample `trend`: about **8.3 MB per model** at 50k rows. Forecasting never reads either one. Dropping them (`slim_for_forecasting`) gives about **7.5 KB per model** at 50k rows and 5.1 KB at 5k, with bit-identical forecasts (point and intervals, same RNG seed). 100k models fit in about 510 MB.
- **The "fat model" is a feature table, not an artifact.** The registered model (`prophet_router`) holds no per-group weights. Because it's logged with `fe.log_model` and a `FeatureLookup` on `prophet_models`, Feature Engineering joins `model_json` by `group_name` for you: from the Delta table in `fe.score_batch` (path 2), and from the Lakebase online table at the serving endpoint (path 3). Callers send only `group_name` and `horizon`.
- **Retraining is a row upsert.** `fe.write_table(mode="merge")` replaces a group's row, so the router doesn't need a new version.
- **Online and offline agree.** The endpoint's forecast matched `score_batch` for the same group (same timestamps, `yhat` within 1e-6). At 100k groups, endpoint queries for groups across the range took 346–815 ms round trip from a laptop.

Limitations:

- Paths 2 and 3 write the same `prophet_models` table, so don't run them at the same time on one schema: concurrent merges fail with `ConcurrentAppendException`. Run them one after the other, or give each its own schema.
- A slim model only forecasts forward. It can't predict in-sample, plot history, or cross-validate. Keep the training data if you need those.
- Path 3's Lakebase online store and endpoint don't scale to zero, so they bill while they exist. The last cell of `03_save_online_inference` deletes them; it's commented out so a run doesn't tear down what it just built.
- Endpoint latency wasn't measured server-side; the round trips above include network and client start-up.
- After retraining, the online table serves new models once its TRIGGERED sync runs. Rerunning path 3 against an existing online table (`fe.publish_table` a second time) hasn't been tested yet.
- The data is synthetic. Fit time, and so run time, depends on the model and on rows per group; see the partitioning comment in `01_no_save_forecast`.

## How to run

1. **Get the code into your workspace.** Clone this repo into a [Databricks Git folder](https://docs.databricks.com/repos/index.html) and open `solutions/scaling-forecasting`.
2. **Attach compute.** Serverless works for every notebook; classic compute works too. Each notebook installs its own pinned libraries in its first cell.
3. **Pick a catalog and schema.** Every notebook has `catalog` / `schema` widgets (defaults `main` / `scaling_forecasting`). You need `USE CATALOG` and `CREATE SCHEMA` on the catalog, or an existing schema where you can `CREATE TABLE` and `CREATE MODEL`. Path 3 also needs permission to create online stores and serving endpoints.
4. **Run `00_generate_timeseries_data`** with the `n_groups` and `rows_per_group` you want (defaults: 10 groups × 50k hourly rows). To use your own data, skip it and set `source_table` in the path notebook to a table with `ds` (timestamp), `group_name` and `y` columns, and `n_groups` to its number of groups (it's also the partition count).
5. **Run one path notebook** with the same `n_groups`:

| Notebook | What you get |
|---|---|
| `01_no_save_forecast` | `forecasts_<source>`: `n_groups × horizon` rows |
| `02_save_offline_inference` | `prophet_models` (one row per group) and `prophet_router@prod`; prints `score_batch: <n> groups × 14 points` |
| `03_save_online_inference` | Path 2's table and router, plus `prophet_models_online` and the `scaling-forecasting-router` endpoint; prints `<group>: endpoint forecast matches the stored model on 14 points` |

6. **After path 3,** uncomment and run the last cell of `03_save_online_inference` to delete the endpoint and online store.

Call the path 3 endpoint with only the group and horizon:

```json
{"dataframe_records": [{"group_name": "group_00001", "horizon": 14}]}
```

### Files

| File | Purpose |
|---|---|
| `00_generate_timeseries_data.py` | Shared synthetic hourly series; `skew=true` gives log-normal group sizes. Reuses the table if it already matches |
| `01_no_save_forecast.py` | Path 1 |
| `02_save_offline_inference.py` | Path 2: slim JSON → feature table → router → `fe.score_batch` |
| `03_save_online_inference.py` | Path 3: slim JSON → feature table → Lakebase → router → serving endpoint; last cell (commented out) cleans up |
| `lib/forecasting.py` | Per-group fit, forecast and slim-JSON functions |
| `lib/router_model.py` | Router model (MLflow models-from-code) with an LRU cache of loaded models |
| `docs/paths.svg` | The diagram above |

Maintainers: `databricks.yml` and `resources/` run each path as a job (`databricks bundle run no_save|save_offline|save_online`), `tests/` holds the unit tests for `lib/` (`pytest tests`), and `docs/make_paths.py` rebuilds the diagram.
