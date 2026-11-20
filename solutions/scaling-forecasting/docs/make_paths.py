"""Builds docs/paths.svg: the three paths (forecast only, save → batch, save → online), each its own flow color,
with every box's first subheading naming the Databricks feature underneath, next to its logo.

Uses the diagramsmith skill. Run from solutions/scaling-forecasting/:
  uv run --no-project --with fonttools --with brotli python docs/make_paths.py [--theme databricks] [--png]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "diagramsmith" / "scripts"))
from diagramsmith import HEAD, Diagram, args, centered, node_height  # noqa: E402

DELTA, FEATURES = ("primary-icon-orange-delta-table.svg", 1.0), ("primary-icon-orange-feature-store.svg", 1.0)
SPARK, MLFLOW = ("apache-spark-logo.svg", 414 / 215), ("mlflow-logo-black-rgb.svg", 954 / 408)
LAKEBASE, SERVING = ("lakebase-icon-full-color.svg", 1.0), ("primary-icon-orange-productionize.svg", 1.0)

opt = args(Path(__file__).parent / "paths.svg")
TOP, BOTTOM = 96, 570
d = Diagram(1400, 625, opt.theme, flows={
    "p1": "Path 1 · train and forecast",
    "p2": "Path 2 · save → batch scoring",
    "p3": "Path 3 · save → online serving",
    "lineage": "router's feature lineage",
})
# panel x, width; nodes sit 15 px inside; arrows turn only in the gaps between panels
P1, P2, P3, P4 = (20, 220), (275, 250), (560, 265), (860, 520)
BUS1, BUS3 = 257, 842
ROW_A, ROW_B, ROW_BATCH = 228, 366, 498  # node mid-lines: path 1, save + path 2 (red), path 3 (blue)
BUS2 = 542  # gap between train and store, where the save lines drop to the models table


def col(p):
    return p[0] + 15, p[1] - 30


def node(name, feature, icon, x, mid, w, *lines):
    """A box whose first subheading is the Databricks feature, with its logo beside it."""
    top = centered(mid, node_height(len(lines) + 1))
    box = d.node(None, name, x, top, w, "", *lines)
    file, aspect = icon
    ih, gap = 16, 6
    iw = ih * aspect
    tw = len(feature) * d.body_char * 0.85  # body_char is generous; 0.85 is close to the real width
    left = x + w / 2 - (iw + gap + tw) / 2
    d.img(file, left, top + 52 - 12, ih, aspect)
    d.sub(left + iw + gap, top + 52, feature, color="text", anchor="start", weight=500)
    return box


d.header("Three paths: forecast only, save and batch-score, or save and serve",
         "Each box names the Databricks feature underneath it. Saved models are small table rows that one "
         "router model looks up automatically, online and in batch.")
d.legend()

# --- 1. Data -------------------------------------------------------------------------------------------------------
d.panel(P1[0], TOP, P1[1], BOTTOM - TOP, "1 · DATA", "one series per group", tint="warm")
x, w = col(P1)
src = node("Time series", "Delta table", DELTA, x, (ROW_A + ROW_B) / 2, w, "date · group key · value")

# --- 2. Train ------------------------------------------------------------------------------------------------------
d.panel(P2[0], TOP, P2[1], BOTTOM - TOP, "2 · TRAIN", "fit every group in parallel")
x, w = col(P2)
train_a = node("Fit and forecast", "Spark pandas UDF", SPARK, x, ROW_A, w, "fit → forecast horizon",
               "every run retrains")
train_b = node("Fit and serialize", "Spark pandas UDF", SPARK, x, ROW_B, w, "fit → slim serialized model",
               "a few KB per group")
d.label(x + w / 2, train_a[0] - 10, "Path 1 · no saved models")
d.label(x + w / 2, train_b[0] - 10, "Paths 2 + 3 · save models")
# leaving the source: path 1 goes up; paths 2 and 3 go down, the lower one turning first so they never cross
d.arrow(f"M{src[4]} {src[2] - 10} H{BUS1 + 4} V{train_a[2]} H{x - HEAD}", "p1")
d.arrow(f"M{src[4]} {src[2] + 2} H{BUS1} V{train_b[2] - 4} H{x - HEAD}", "p2")
d.arrow(f"M{src[4]} {src[2] + 10} H{BUS1 - 8} V{train_b[2] + 4} H{x - HEAD}", "p3")
d.sub(x + w / 2, ROW_BATCH - 6, "one partition per group")
d.sub(x + w / 2, ROW_BATCH + 12, "serverless or classic compute")

# --- 3. Store ------------------------------------------------------------------------------------------------------
# red (path 2) stays above blue (path 3) end to end: router on the save row, models table below it, batch scoring
# top right, the online lane along the bottom
d.panel(P3[0], TOP, P3[1], BOTTOM - TOP, "3 · STORE", "Unity Catalog")
x, w = col(P3)
forecasts = node("Forecasts table", "Delta table", DELTA, x, ROW_A, w, "group · date · forecast · bounds",
                 "read by dashboards and apps")
router = node("Router model", "MLflow model in UC", MLFLOW, x, ROW_B, w, "re-logged only when code changes",
              "no per-group weights")
models = node("Models feature table", "UC feature table", FEATURES, x, ROW_BATCH, w, "primary key: group key",
              "serialized model · CDF on")
d.arrow(f"M{train_a[4]} {train_a[2]} H{x - HEAD}", "p1")
# the lower (blue) line turns down first, and the right (red) one turns in first, so they never cross
d.arrow(f"M{train_b[4]} {train_b[2] - 4} H{BUS2 + 4} V{models[2] - 4} H{x - HEAD}", "p2")
d.arrow(f"M{train_b[4]} {train_b[2] + 4} H{BUS2 - 4} V{models[2] + 4} H{x - HEAD}", "p3")
d.varrow(x + w / 2, router[1], models[0], "lineage")
d.label(x + w / 2 + 10, (router[1] + models[0]) / 2 + 4, "feature lookup", anchor="start")

# --- 4. Serve ------------------------------------------------------------------------------------------------------
# offline lane (path 2): batch scoring top right; online lane (path 3): Lakebase → endpoint along the bottom
d.panel(P4[0], TOP, P4[1], BOTTOM - TOP, "4 · SERVE", "both run the router model")
GAP = 40
w = (P4[1] - 30 - GAP) / 2
x_on, x_ep = P4[0] + 15, P4[0] + 15 + w + GAP
BUS4 = x_ep - GAP / 2  # between the online table and the endpoint
batch = node("Batch scoring", "Feature Engineering", FEATURES, x_ep, ROW_B, w, "input: group keys only",
             "joins the Delta table")
online = node("Online feature table", "Lakebase online store", LAKEBASE, x_on, ROW_BATCH, w,
              "triggered sync from Delta", "no scale-to-zero")
endpoint = node("Serving endpoint", "Model Serving", SERVING, x_ep, ROW_BATCH, w, "in: group key, horizon",
                "lookup → predict")
# path 2: the router runs in batch, and the Delta join rises on the bus to meet it, so one red line enters the box
d.arrow(f"M{router[4]} {batch[2]} H{x_ep - HEAD}", "p2")
d.arrow(f"M{models[4]} {models[2] - 10} H{BUS3} V{batch[2]}", "p2", head=False)
d.label(x_on + w / 2, batch[2] - 10, "router + Delta join, in batch")
# path 3: the router is deployed to the endpoint, meeting the Lakebase lookup between the online boxes
d.arrow(f"M{models[4]} {models[2]} H{x_on - HEAD}", "p3")
d.arrow(f"M{online[4]} {online[2]} H{x_ep - HEAD}", "p3")
d.arrow(f"M{router[4]} {router[2] + 20} H{BUS4} V{endpoint[2]}", "p3", head=False)
d.label(x_on + w / 2, router[2] + 38, "router deployed to the endpoint")

d.sub(30, BOTTOM + 32, "Retraining upserts rows in the models table: the router's version doesn't change, and the "
                       "online table picks up new models on its next sync.", anchor="start")

d.save(opt.out, png=opt.png)
