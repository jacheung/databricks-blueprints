"""Builds docs/paths.svg: the no-save path (forecasts only) and the save path (models as feature-table rows,
one router model, automatic lookups online via Lakebase and in batch via score_batch).

Uses the diagramsmith skill. Run from solutions/scaling-forecasting/:
  uv run --no-project --with fonttools --with brotli python docs/make_paths.py [--theme databricks] [--png]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "diagramsmith" / "scripts"))
from diagramsmith import HEAD, Diagram, args, centered, node_height  # noqa: E402

DELTA, TRAIN, FEATURES = ("primary-icon-orange-delta-table.svg", "primary-icon-orange-model-training.svg",
                          "primary-icon-orange-feature-store.svg")
LAKEBASE, SERVING, MLFLOW = ("lakebase-icon-full-color.svg", "primary-icon-orange-productionize.svg",
                             ("mlflow-logo-black-rgb.svg", 954 / 408))

opt = args(Path(__file__).parent / "paths.svg")
TOP, BOTTOM = 96, 560
d = Diagram(1400, 615, opt.theme, flows={
    "write": "training writes",
    "publish": "Delta → Lakebase sync",
    "lookup": "automatic lookup by group_name",
    "lineage": "router's feature lineage",
})
# panel x, width; nodes sit 15 px inside; arrows turn only in the gaps between panels
P1, P2, P3, P4, P5 = (20, 220), (275, 250), (560, 265), (860, 270), (1165, 215)
BUS1, BUS3 = 257, 842
ROW_A, ROW_B, ROW_BATCH = 224, 360, 488  # node mid-lines: no-save path, save path, batch inference


def col(p):
    return p[0] + 15, p[1] - 30


def at(mid, n_lines):
    return centered(mid, node_height(n_lines))


d.header("Three paths: forecast only, save and batch-score, or save and serve",
         "A Spark pandas UDF fits each group. Saved models are ~8 KB rows that one router model "
         "looks up automatically, online and in batch.")
d.legend()

# --- 1. Data -------------------------------------------------------------------------------------------------------
d.panel(P1[0], TOP, P1[1], BOTTOM - TOP, "1 · DATA", "one series per group", tint="warm")
x, w = col(P1)
src = d.node(DELTA, "timeseries_*", x, at((ROW_A + ROW_B) / 2, 2), w, "ds · group_name · y", "one Delta table")

# --- 2. Train ------------------------------------------------------------------------------------------------------
d.panel(P2[0], TOP, P2[1], BOTTOM - TOP, "2 · TRAIN", "Spark applyInPandas, per group")
x, w = col(P2)
train_a = d.node(TRAIN, "train_and_forecast", x, at(ROW_A, 2), w, "fit → forecast horizon", "every run retrains")
train_b = d.node(TRAIN, "train_to_json", x, at(ROW_B, 2), w, "fit → slim Prophet JSON", "~8 KB per group")
d.label(x + w / 2, train_a[0] - 10, "Path 1 · no saved models")
d.label(x + w / 2, train_b[0] - 10, "Paths 2 + 3 · save models")
d.arrow(f"M{src[4]} {src[2] - 8} H{BUS1 - 4} V{train_a[2]} H{x - HEAD}", "write")
d.arrow(f"M{src[4]} {src[2] + 8} H{BUS1 + 4} V{train_b[2]} H{x - HEAD}", "write")
d.sub(x + w / 2, ROW_BATCH - 6, "one partition per group")
d.sub(x + w / 2, ROW_BATCH + 12, "serverless or classic compute")

# --- 3. Store ------------------------------------------------------------------------------------------------------
d.panel(P3[0], TOP, P3[1], BOTTOM - TOP, "3 · STORE", "Unity Catalog")
x, w = col(P3)
forecasts = d.node(DELTA, "forecasts_<source>", x, at(ROW_A, 2), w, "group_name · ds · yhat · bounds",
                   "read by dashboards and apps")
models = d.node(FEATURES, "prophet_models", x, at(ROW_B, 2), w, "feature table · PK group_name",
                "model_json · CDF on", accent=True)
router = d.node(MLFLOW, "prophet_router", x, at(ROW_BATCH, 2), w, "@prod · logged once, fe.log_model",
                "no per-group weights")
d.arrow(f"M{train_a[4]} {train_a[2]} H{x - HEAD}", "write")
d.arrow(f"M{train_b[4]} {train_b[2]} H{x - HEAD}", "write")
d.varrow(x + w / 2, router[0], models[1], "lineage")
d.label(x + w / 2 + 10, (models[1] + router[0]) / 2 + 4, "FeatureLookup", anchor="start")

# --- 4. Online store -----------------------------------------------------------------------------------------------
d.panel(P4[0], TOP, P4[1], BOTTOM - TOP, "4 · ONLINE STORE", "Lakebase · path 3 only", tint="cool")
x, w = col(P4)
online = d.node(LAKEBASE, "prophet_models_online", x, at(ROW_B, 2), w, "fe.publish_table, TRIGGERED", "no scale-to-zero")
d.arrow(f"M{models[4]} {models[2] - 8} H{x - HEAD}", "publish")

# --- 5. Inference --------------------------------------------------------------------------------------------------
d.panel(P5[0], TOP, P5[1], BOTTOM - TOP, "5 · INFERENCE", "both run prophet_router @prod")
x, w = col(P5)
endpoint = d.node(SERVING, "Serving endpoint", x, at(ROW_B, 2), w, "in: group_name, horizon",
                  "lookup → predict")
batch = d.node(SERVING, "fe.score_batch", x, at(ROW_BATCH, 2), w, "input: keys only", "joins the Delta table")
d.label(x + w / 2, endpoint[0] - 10, "Path 3 · online")
d.label(x + w / 2, batch[0] - 10, "Path 2 · offline")
d.arrow(f"M{online[4]} {online[2]} H{x - HEAD}", "lookup")
d.arrow(f"M{models[4]} {models[2] + 8} H{BUS3} V{batch[2]} H{x - HEAD}", "lookup")

d.sub(30, BOTTOM + 32, "Retraining upserts rows in prophet_models: the router's version doesn't change, and the "
                       "online table picks up new models on its next sync.", anchor="start")

d.save(opt.out, png=opt.png)
