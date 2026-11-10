# Databricks notebook source
# DBTITLE 1,Simulate shopper sessions over the ESCI catalog
# MAGIC %md
# MAGIC # Simulate shopper sessions over the ESCI catalog
# MAGIC
# MAGIC ESCI has real queries and relevance labels but no sessions. This notebook plants a latent **intent**
# MAGIC (category, price tier, brand) in most sessions and simulates search → click → back → add-to-cart events,
# MAGIC so the L2 rerankers have personalization signal to find and we can measure it against known ground truth.
# MAGIC The click model is `rts_data.datagen.simulate`: P(click) = base rate for the ESCI label × how well the product fits
# MAGIC the intent.
# MAGIC
# MAGIC | Table | Grain | Used by |
# MAGIC |---|---|---|
# MAGIC | `session_events` | one row per event (CDF on) | user/item Feature View source; replayed to Kafka for session features (kafka_stream) |
# MAGIC | `qac_impressions` | one row per submitted search; `outcome` 2 = click/cart, 1 = submitted only | QAC L1 popularity (03), QAC L2 labels (05) |
# MAGIC | `serp_impressions` | one row per (search, ESCI-judged product) with `clicked`/`carted` | SERP L2 labels (06) |
# MAGIC | `sim_sessions` | one row per session: planted intent (category, brand, price target) and split | evaluation only; never a feature |
# MAGIC
# MAGIC Events cover the `data.sim.history_days` days ending yesterday (UTC), so live online user/item features have
# MAGIC data in their windows; sessions in the last `test_days` are the test set. Rerun with `rebuild_data=true` to refresh.
# MAGIC
# MAGIC **Skipped when `session_events` exists** unless `rebuild_data=true`. Rebuilding replaces `session_events`,
# MAGIC which breaks Feature View materializations reading it, so run `teardown_job` first.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+. Runs on the driver (~2M events).

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
from datetime import date

import pandas as pd
from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_data.datagen import Catalog, intent_lift, intent_lift_problems, simulate
from rts_shared.config import category_medians, load_config, sim_dates, table
from rts_shared.events import EVENT_FIELDS

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
dbutils.widgets.dropdown("rebuild_data", "false", ["false", "true"])  # replace an existing session_events
CFG = load_config(dbutils.widgets.get("config_path"))
if spark.catalog.tableExists(table(CFG, "session_events")) and dbutils.widgets.get("rebuild_data") != "true":
    dbutils.notebook.exit(json.dumps({"skipped": f"{table(CFG, 'session_events')} exists; set rebuild_data=true"}))
SIM = {**CFG["data"]["sim"], **sim_dates(CFG, date.today())}
print({k: SIM[k] for k in ("start_date", "end_date", "test_split_after")})

# COMMAND ----------

# DBTITLE 1,Build the catalog on the driver
judged_rows = (spark.table(table(CFG, "labels"))
               .join(spark.table(table(CFG, "products")), "product_id")
               .where("category IS NOT NULL")
               .select("query_id", "product_id", "esci_label", "category", F.coalesce("brand", F.lit("")).alias("brand"),
                       "price")
               .toPandas())
judged = {}
for r in judged_rows.itertuples(index=False):
    judged.setdefault(r.query_id, []).append((r.product_id, r.esci_label, r.category, r.brand, r.price))
queries = [tuple(r) for r in spark.table(table(CFG, "queries")).where("category IS NOT NULL")
           .select("query_id", "query", "category").toPandas().itertuples(index=False)]
catalog = Catalog.build(queries, judged, category_medians(CFG))
print(f"{len(catalog.queries):,} queries, {len(judged_rows):,} judgements")

# COMMAND ----------

# DBTITLE 1,Simulate
events = pd.DataFrame(simulate(catalog, SIM))
print(f"{events.session_id.nunique():,} sessions, {len(events):,} events")
events.event_type.value_counts()

# COMMAND ----------

# DBTITLE 1,Write session_events (schema shared with the Kafka payload)
SESSION_EVENTS = table(CFG, "session_events")
EVENT_COLUMNS = [name for name, _ in EVENT_FIELDS]
REQUIRED = {"session_id", "event_id", "event_time", "event_type"}
columns = ", ".join(f"{n} {t.upper()}{' NOT NULL' if n in REQUIRED else ''}" for n, t in EVENT_FIELDS)
spark.sql(f"CREATE OR REPLACE TABLE {SESSION_EVENTS} ({columns}) TBLPROPERTIES (delta.enableChangeDataFeed = true)")
(spark.createDataFrame(events[EVENT_COLUMNS])
 .withColumn("query_id", F.col("query_id").cast("bigint"))
 .withColumn("price", F.when(~F.isnan("price"), F.col("price")))  # pandas NaN -> NULL on search/back events
 .write.mode("append").saveAsTable(SESSION_EVENTS))
session_events = spark.table(SESSION_EVENTS)

# COMMAND ----------

# DBTITLE 1,Sessions, splits and impressions
sessions = (spark.createDataFrame(events.groupby("session_id", as_index=False)
                                  .agg(user_id=("user_id", "first"), intent_category=("intent_category", "first"),
                                       intent_brand=("intent_brand", "first"),
                                       intent_price_target=("intent_price_target", "first"),
                                       started_at=("event_time", "min")))
            .withColumn("split", F.when(F.col("started_at") >= F.lit(SIM["test_split_after"]).cast("timestamp"), "test")
                                  .otherwise("train")))
sessions.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table(CFG, "sim_sessions"))
sessions = spark.table(table(CFG, "sim_sessions"))

engaged = (session_events.where("event_type IN ('click', 'add_to_cart')")
           .groupBy("search_id", "product_id")
           .agg(F.max(F.col("event_type") == "click").alias("clicked"),
                F.max(F.col("event_type") == "add_to_cart").alias("carted")))
searches = (session_events.where("event_type = 'search'")
            .join(sessions.select("session_id", "split"), "session_id"))

qac = (searches.join(engaged.select("search_id").distinct().withColumn("engaged", F.lit(True)), "search_id", "left")
       .select("search_id", "session_id", "user_id", "event_time", "device", "query_id", "query", "category",
               F.when(F.col("engaged"), 2).otherwise(1).alias("outcome"), "split"))
qac.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table(CFG, "qac_impressions"))

serp = (searches.select("search_id", "session_id", "user_id", "event_time", "device", "query_id", "split")
        .join(spark.table(table(CFG, "labels")).select("query_id", "product_id", "esci_label"), "query_id")
        .join(engaged, ["search_id", "product_id"], "left")
        .fillna(False, subset=["clicked", "carted"]))
serp.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table(CFG, "serp_impressions"))

# COMMAND ----------

# DBTITLE 1,Check the planted intent shows up
# Category is fixed by the query, so inside one result list personalization has to come from brand and price:
# in intent sessions, clicked products should match the intent brand more often than the products shown, and sit
# closer to the intent price. Searches should also be abandoned often enough for the QAC outcome label to carry
# signal, and Exact matches should get the highest click-through.
lift = intent_lift(events.to_dict("records"), catalog.judged)
ctr = (spark.table(table(CFG, "serp_impressions")).groupBy("esci_label")
       .agg(F.avg(F.col("clicked").cast("double")).alias("ctr")).toPandas().set_index("esci_label").ctr.round(4).to_dict())
outcomes = {int(r["outcome"]): r["count"] for r in
            spark.table(table(CFG, "qac_impressions")).groupBy("outcome").count().collect()}
engaged_share = outcomes.get(2, 0) / sum(outcomes.values())
split_counts = {r["split"]: r["count"] for r in sessions.groupBy("split").count().collect()}

summary = {
    "sessions": int(events.session_id.nunique()),
    "events": {k: int(v) for k, v in events.event_type.value_counts().items()},
    "dates": {k: SIM[k] for k in ("start_date", "end_date", "test_split_after")},
    "sessions_by_split": split_counts,
    "guest_session_share": round(float(events.groupby("session_id").user_id.first().isna().mean()), 3),
    "engaged_search_share": round(engaged_share, 3),
    "intent_lift": lift,
    "ctr_by_esci_label": ctr,
}
print(json.dumps(summary, indent=2))
assert 0.35 <= engaged_share <= 0.65, "share of searches with a click or cart is outside 35-65%"
problems = intent_lift_problems(lift)
assert not problems, "; ".join(problems)
assert ctr["E"] > ctr["S"] > ctr["I"], "Exact matches should be clicked most"
dbutils.notebook.exit(json.dumps(summary))
