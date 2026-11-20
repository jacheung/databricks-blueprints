# Scaling forecasting: many local models

Forecasting often means one model per group, not one model for everything: a model per store × SKU in retail, per meter in utilities, per route in logistics. These **local models** are small and quick to fit one at a time. The trouble starts at 10k–100k+ of them, and it shows up in two places: training, and (if you keep the models) inference.

## The two bottlenecks

### 1. Training: the libraries are single-node

Prophet, statsmodels (ARIMA, ETS) and scikit-learn fit one model in one Python process on one machine. Fit 100k groups one after another at a few seconds each and it takes days. The fit itself doesn't need to be distributed. The *groups* do: a framework like **Spark** or **Ray** sends each group's rows to a worker and runs the single-node fit there, across the whole cluster at once.

### 2. Inference: too many models to manage

If you forecast right after training, you never keep the models and there's no second bottleneck. Once you keep them (to forecast more often than you retrain, to audit or roll back, or to serve on demand), the model-per-endpoint pattern stops working:

- **One endpoint per model is untenable.** 100k endpoints means 100k deployments to start, monitor, scale and roll back, past workspace endpoint quotas and paying for each one's idle capacity.
- **Registry APIs become the limit.** Registering 100k models in MLflow, and 100k new versions on every retrain, runs into API rate limits, as does loading them back one by one.

The fix is a **router model**: register *one* model and deploy *one* endpoint. The per-group models are rows in a table keyed by group. For each request, the router looks up that group's model, deserializes it and forecasts. Retraining a group is a row update, not a new deployment.

![Prototype vs production, training on top and inference below. Prototype: one Python process fits 100k groups one after another, then each group gets its own endpoint with its weights packaged inside, so a request for group 42 goes to endpoint g42 and its forecast comes back from there. Production: each group's rows go to a worker inside a Spark or Ray cluster and the workers fit in parallel, then one router endpoint takes group 42 and a horizon, fetches g42's artifact from a store holding one artifact per group, loads it and returns the forecast](docs/scaling.svg)

## How it maps to Databricks

| Need | Databricks piece |
|---|---|
| Parallelize single-node fits | A Spark grouped pandas UDF, one partition per group, on serverless. For per-group hyperparameter tuning or multi-core fits, use [Ray on Databricks](https://docs.databricks.com/machine-learning/ray/) (classic compute); for GPU fits, use [AI Runtime](https://docs.databricks.com/machine-learning/ai-runtime/) (serverless GPUs, which also [run Ray](https://docs.databricks.com/machine-learning/ai-runtime/ray)) |
| Store 100k models | A Unity Catalog feature table: one row per group, keyed by group, with the model as a slim serialized string |
| One model in front of all of them | A router model, logged with Feature Engineering and a feature lookup on the models table. It holds no per-group weights |
| Offline (batch) inference | Feature Engineering batch scoring with group keys only; it joins each group's model from the Delta table |
| Online inference | The feature table published to a Lakebase online table, behind one Model Serving endpoint. Callers send only the group key and horizon |

![Three paths from one per-group time series table in a Delta table, each drawn in its own color. Path 1: a Spark pandas UDF fits and forecasts each group and writes forecasts to a Delta table. Paths 2 and 3 share the save steps: a Spark pandas UDF fits each group and writes its slim serialized model to a Unity Catalog feature table, and one router model, an MLflow model in Unity Catalog, is logged with feature lineage to it. Path 2: Feature Engineering batch scoring runs the router model and joins each group's model from the Delta table. Path 3: the router model is deployed to a Model Serving endpoint, the feature table syncs to a Lakebase online store, and the endpoint looks up each group's model there by group key](docs/paths.svg)

What we found with Prophet on serverless (10 groups × 50k hourly rows, then 100k groups × 5k rows):

- **Training scales without sizing anything.** At 100k groups (500M rows), path 1 fit and forecast all 100k models in about 7 minutes. One partition per group (100k partitions) caused no problems.
- **Saving and serving add minutes, not hours.** Training into the feature table and publishing to Lakebase took about 11 minutes. Logging the router, batch-scoring all 100k groups and bringing up the endpoint took about 21 minutes, most of it endpoint start-up.
- **Slim models keep the table small.** Prophet's `model_to_json` embeds the training history and the in-sample trend, about 8.3 MB per model at 50k rows. Forecasting never reads either. Dropping them gives 5–8 KB per model with bit-identical forecasts, so 100k models take about 510 MB.
- **Online and offline agree.** The endpoint's forecast matched `score_batch` for the same group (`yhat` within 1e-6). Round trips from a laptop took 346–815 ms at 100k groups.

## Three harnesses

The three paths are ready-made harnesses. All three start from one table of per-group series (`ds`, `group_name`, `y`). Each is one notebook you can run on its own:

| Path | Use when | Creates |
|---|---|---|
| **Path 1 · Train and forecast** · `01_no_save_forecast` | You retrain every time you forecast (a nightly job, say) | `forecasts_<source>`, plus a backtest table |
| **Path 2 · Save → offline inference** · `02_save_offline_inference` | You forecast more often than you retrain, or need to audit and roll back models; batch is enough | `prophet_models`, `prophet_router@prod`, batch forecasts |
| **Path 3 · Save → online inference** · `03_save_online_inference` | An app needs one group's forecast on demand | Path 2's table and router, plus a Lakebase online table and a serving endpoint (both bill while they exist) |

**Running them well** ([harness guide](docs/harness-guide.md)):

- **Partition by fit time.** Use one partition per group when a fit takes seconds or more. For sub-second fits, use 2–4× the core count. With very many short fits, cap at 10–20× the core count.
- **Backtest before trusting forecasts.** Path 1 holds out each group's last `horizon` rows and records MAE, RMSE and 80%-interval coverage. Read p50 and p90 across groups, not just the mean. It's one window: repeat over several cutoffs (rolling origin) and horizons to see how stable accuracy is.
- **Retraining is an upsert.** `fe.write_table(mode="merge")` replaces a group's row, and the router needs a new version only when code changes. Merges never delete, so remove retired groups yourself. The online table picks up new models after its TRIGGERED sync.
- **Don't run paths 2 and 3 at the same time on one schema.** Both merge into `prophet_models`, and concurrent merges fail with `ConcurrentAppendException`.
- **Clean up path 3.** The endpoint and Lakebase store don't scale to zero. The last cell of `03_save_online_inference` deletes them; it's commented out.

**Bringing your own model** ([guide](docs/bring-your-own-model.md)):

- **Replace four functions in `lib/forecasting.py`:**
  - `_fit(pdf)`: one group's rows → a fitted model.
  - `_forecast(model, horizon)`: returns `ds`, `yhat`, `yhat_lower`, `yhat_upper`.
  - `serialize_model` and `deserialize_model`: the model ↔ a small string.
  - Also set `FREQ` to your data's frequency, and pin your libraries in `lib/requirements.txt`.
- **Everything else stays.** The notebooks, the backtest, the feature table and the router are built on those four functions. `lib/router_model.py` has no model-specific code.
- **Parameters fall into three kinds:**
  - Fitted state and per-group hyperparameters live inside the serialized string.
  - Request-time parameters (`horizon`) are request columns.
  - Per-group metadata (model type, version) goes in extra feature-table columns added to the `FeatureLookup`.
- **What reaches past `forecasting.py`:**
  - Models without prediction intervals: change the output schemas.
  - Binary model formats: base64 them into the string column.
  - Exogenous regressors: future values have to come in with the request. The harness doesn't do that yet.
- **Local models only.** One global model across all groups doesn't need a router; register it as an ordinary model.

## Limitations

- A slim Prophet model only forecasts forward. It can't predict in-sample, plot history or cross-validate; keep the training data if you need those.
- Endpoint latency wasn't measured server-side. The round trips above include network and client start-up.
- Rerunning path 3 against an existing online table (`fe.publish_table` a second time) hasn't been tested yet.
- The data is synthetic. Run time depends on the model and on rows per group.

## How to run

1. **Get the code into your workspace.** Clone this repo into a [Databricks Git folder](https://docs.databricks.com/repos/index.html) and open `solutions/scaling-forecasting`.
2. **Attach compute.** Serverless works for every notebook; classic compute works too. Each notebook installs its own pinned libraries in its first cell.
3. **Pick a catalog and schema.** Every notebook has `catalog` / `schema` widgets (defaults `main` / `scaling_forecasting`). You need `USE CATALOG` and `CREATE SCHEMA` on the catalog, or an existing schema where you can `CREATE TABLE` and `CREATE MODEL`. Path 3 also needs permission to create online stores and serving endpoints.
4. **Run `00_generate_timeseries_data`** with the `n_groups` and `rows_per_group` you want (defaults: 10 groups × 50k hourly rows). To use your own data, skip it and set `source_table` in the path notebook to a table with `ds` (timestamp), `group_name` and `y` columns, and `n_groups` to its number of groups (it's also the partition count).
5. **Run one path notebook** with the same `n_groups`:

| Notebook | What you get |
|---|---|
| `01_no_save_forecast` | `forecasts_<source>`: `n_groups × horizon` rows; `backtest_<source>`: one row per group with MAE, RMSE and 80%-interval coverage on the last `horizon` rows |
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
| `01_no_save_forecast.py` | Path 1, with a single-window backtest |
| `02_save_offline_inference.py` | Path 2: slim JSON → feature table → router → `fe.score_batch` |
| `03_save_online_inference.py` | Path 3: slim JSON → feature table → Lakebase → router → serving endpoint; last cell (commented out) cleans up |
| `lib/forecasting.py` | Per-group fit, forecast, serialize and backtest functions; the four model-specific ones are where your model goes |
| `lib/router_model.py` | Router model (MLflow models-from-code) with an LRU cache of loaded models |
| `lib/requirements.txt` | Model library pins, installed by the notebooks and packaged with the router |
| `docs/harness-guide.md`, `docs/bring-your-own-model.md` | The longer guides |
| `docs/scaling.svg`, `docs/paths.svg` | The diagrams above; each is rebuilt by its `docs/make_*.py` |

Maintainers: `databricks.yml` and `resources/` run each path as a job (`databricks bundle run no_save|save_offline|save_online`), `tests/` holds the unit tests for `lib/` (`pytest tests`), and `docs/make_*.py` rebuild the diagrams.
