# Harness guide: running per-group forecasting at scale

How to run the three paths well and what to watch for. Everything here holds whatever model you
plug in; swapping the model is covered in [bring-your-own-model.md](bring-your-own-model.md).

## 1. Pick a path

| If… | Use | Why |
|---|---|---|
| You retrain every time you forecast (a nightly job, say) | Path 1 · `01_no_save_forecast` | Nothing to store or serve; cheapest |
| You forecast more often than you retrain, or need to audit and roll back models, and batch output is enough | Path 2 · `02_save_offline_inference` | Models live in a feature table; `fe.score_batch` forecasts without refitting |
| An app needs one group's forecast on demand | Path 3 · `03_save_online_inference` | Path 2 plus a Lakebase online table and a serving endpoint |

Path 3 includes everything in path 2. Start with path 1 unless you have a reason to keep models.

## 2. Shape the input

- One table with `ds` (timestamp), `group_name` and `y`, one row per group per timestamp.
- A regular frequency that matches `FREQ` in `lib/forecasting.py` (hourly here). Gaps and
  duplicate timestamps go to the model as-is, so clean them first.
- Each group needs more than `horizon` rows, or the backtest has nothing to train on.

## 3. Partition by fit time

Use one partition per group (`repartition(n_groups, "group_name")`) when one fit takes seconds
or more. Prophet takes about 1–350 s per group depending on rows. Per-task overhead is tiny next
to that, and a large group can't hold up the small groups packed in with it.

- Sub-second fits: overhead dominates. Use about 2–4× the core count.
- Very many groups: 100k partitions ran fine on serverless with ~2 s fits. Well beyond that, or
  with short fits, cap at about 10–20× the core count.
- Uniform group sizes: any of these behave about the same. Packing only hurts when sizes vary
  (`00_generate_timeseries_data` with `skew=true` lets you test that).

## 4. Compute

Serverless works for every notebook and needs no sizing; that's what the numbers in the README
come from. Classic compute works too. Spark `applyInPandas` matched Ray on equal cores
([benchmarks/spark-vs-ray-forecasting](../../../benchmarks/spark-vs-ray-forecasting/)), so
there's no need for a Ray cluster.

## 5. Check accuracy before trusting the forecasts

Path 1 ends with a backtest at your `horizon`: each group's last `horizon` rows are held out,
and `backtest_<source>` gets MAE, RMSE and 80%-interval coverage per group.

- Read the spread across groups (p50, p90), not just the average. A few bad groups hide in a mean.
- Coverage well below 0.8 means the intervals are too narrow, so don't use them for safety stock.
- **This is one window.** It shows the error from one cutoff, not how stable it is. Before going
  to production, repeat it over several cutoffs (a rolling-origin backtest, e.g.
  `prophet.diagnostics.cross_validation`) to see how much the error moves over time, and over
  several horizons to see where accuracy starts to drop.

## 6. Retrain

- Path 1: rerun the notebook.
- Paths 2 and 3: rerunning training upserts each group's row (`fe.write_table(mode="merge")`).
  The router doesn't need a new version; it needs one only when `lib/` code changes.
- A merge never deletes. Groups missing from the new data keep their old model, so delete
  retired groups from `prophet_models` yourself.
- Path 3's online table uses a TRIGGERED sync, so the endpoint serves new models after that sync
  runs. Rerunning `fe.publish_table` on an existing online table hasn't been tested yet.
- Paths 2 and 3 write the same `prophet_models` table. Running them at the same time on one schema
  fails with `ConcurrentAppendException`, so run them one after the other or use separate schemas.

## 7. Serve (path 3)

- Callers send only `group_name` and `horizon`; the endpoint looks up the model in Lakebase.
- Each replica keeps an LRU cache of 256 deserialized models (`load_model` in
  `lib/router_model.py`). Raise it if many groups are hot and the models are small.
- At 100k groups, round trips from a laptop took 346–815 ms. That wasn't measured server-side.
- The endpoint (`scale_to_zero_enabled=False`) and the Lakebase online store bill while they
  exist. The last cell of `03_save_online_inference` deletes both; it's commented out.
