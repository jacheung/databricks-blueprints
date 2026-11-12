# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Create the wide source table
# MAGIC
# MAGIC Builds `docs_wide`, a synthetic table with three content columns and one shared embedding:
# MAGIC
# MAGIC | Column | Role |
# MAGIC |---|---|
# MAGIC | `id` | Primary key |
# MAGIC | `title` (A) | Used by Index A only |
# MAGIC | `body` (B) | Used by both indexes; the text that gets embedded |
# MAGIC | `category` (C) | Used by Index B only |
# MAGIC | `body_vector` | Self-managed embedding of `body`, computed **once** here |

# COMMAND ----------

dbutils.widgets.text("catalog_name", "main", "Catalog")
dbutils.widgets.text("schema_name", "split_table_vs", "Schema")
dbutils.widgets.text("num_rows", "10000", "Rows to generate")

catalog = dbutils.widgets.get("catalog_name")
schema = dbutils.widgets.get("schema_name")
num_rows = int(dbutils.widgets.get("num_rows"))
fq_schema = f"{catalog}.{schema}"
source_table = f"{fq_schema}.docs_wide"

from narrow_tables import EMBEDDING_MODEL

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fq_schema}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Synthetic documents
# MAGIC Twenty topics, each with a base sentence, plus random detail so every row is distinct.

# COMMAND ----------

from pyspark.sql import functions as F

topics = [
    ("billing", "How to read your monthly invoice and dispute an incorrect charge."),
    ("billing", "Updating the credit card on file and setting up automatic payments."),
    ("security", "Enabling multi-factor authentication and recovering a locked account."),
    ("security", "Rotating API keys and revoking access for former employees."),
    ("networking", "Configuring a VPN tunnel and troubleshooting dropped connections."),
    ("networking", "Setting up DNS records and verifying domain ownership."),
    ("storage", "Choosing a storage tier and setting lifecycle rules to archive old files."),
    ("storage", "Recovering deleted files from snapshots and backups."),
    ("compute", "Right-sizing virtual machines and enabling autoscaling for spiky traffic."),
    ("compute", "Diagnosing out-of-memory errors on long-running batch jobs."),
    ("analytics", "Building a dashboard that tracks weekly active users."),
    ("analytics", "Writing SQL window functions to compute rolling averages."),
    ("ml", "Training a gradient boosted model and tuning its hyperparameters."),
    ("ml", "Serving a model behind a low-latency REST endpoint."),
    ("onboarding", "Inviting teammates to a workspace and assigning roles."),
    ("onboarding", "A first-week checklist for new administrators."),
    ("compliance", "Exporting audit logs for a SOC 2 review."),
    ("compliance", "Configuring data retention to meet GDPR deletion requests."),
    ("support", "Opening a support ticket and choosing the right severity level."),
    ("support", "Escalating an outage and finding the public status page."),
]
details = ["for small teams", "in production", "step by step", "with screenshots",
           "for administrators", "using the CLI", "using the API", "on mobile"]

topics_df = spark.createDataFrame(
    [(i, cat, text) for i, (cat, text) in enumerate(topics)], "topic_id INT, category STRING, base STRING"
)
details_arr = F.array(*[F.lit(d) for d in details])

df = (
    spark.range(num_rows)
    .withColumn("topic_id", (F.col("id") % len(topics)).cast("int"))
    .join(F.broadcast(topics_df), "topic_id")
    .withColumn("detail", F.element_at(details_arr, (F.col("id") % len(details) + 1).cast("int")))
    .select(
        F.col("id").cast("string").alias("id"),
        F.concat(F.initcap("category"), F.lit(" guide #"), F.col("id")).alias("title"),
        F.concat_ws(" ", "base", "detail", F.concat(F.lit("(ref "), F.col("id"), F.lit(")"))).alias("body"),
        "category",
    )
)
df.createOrReplaceTempView("docs_raw")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Embed `body` once and write the source table
# MAGIC CDF is enabled on the wide table only so the comparison index in appendix A1 can sync from it.
# MAGIC The narrow-table pattern itself doesn't need it.

# COMMAND ----------

spark.sql(f"""
    CREATE OR REPLACE TABLE {source_table}
    TBLPROPERTIES (delta.enableChangeDataFeed = true)
    AS SELECT id, title, body, category,
              CAST(ai_query('{EMBEDDING_MODEL}', body) AS ARRAY<FLOAT>) AS body_vector
    FROM docs_raw
""")

display(spark.sql(f"SELECT id, title, body, category, size(body_vector) AS dim FROM {source_table} LIMIT 5"))
print(spark.table(source_table).count(), "rows in", source_table)
