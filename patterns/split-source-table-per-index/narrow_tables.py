"""Split one wide source table into one narrow table per vector search index.

Each narrow table holds the primary key plus only the columns its index needs, and
has the change data feed (CDF) enabled. The refresh MERGE only updates a row when one
of *that table's* columns changed, so an edit to a column an index doesn't use never
reaches that index's CDF, and its next sync has nothing to process.
"""

import datetime

from delta.tables import DeltaTable

PRIMARY_KEY = "id"
EMBEDDING_DIM = 1024
EMBEDDING_MODEL = "databricks-gte-large-en"

# narrow table name -> columns it carries (besides the primary key).
# body_vector is computed once upstream in the wide table and shared by both.
NARROW_TABLES = {
    "docs_idx_title": ["title", "body", "body_vector"],        # Index A: A + B
    "docs_idx_category": ["body", "category", "body_vector"],  # Index B: B + C
}


def create_narrow_tables(spark, fq_schema, source_table):
    """Create each narrow table (empty, CDF on) with the source table's column types."""
    for name, cols in NARROW_TABLES.items():
        spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {fq_schema}.{name}
            TBLPROPERTIES (delta.enableChangeDataFeed = true)
            AS SELECT {PRIMARY_KEY}, {', '.join(cols)} FROM {source_table} WHERE false
        """)


def refresh_narrow_tables(spark, fq_schema, source_table):
    """Upsert each narrow table from the source, writing only rows whose columns changed."""
    for name, cols in NARROW_TABLES.items():
        src = spark.table(source_table).select(PRIMARY_KEY, *cols)
        changed = " OR ".join(f"NOT (t.{c} <=> s.{c})" for c in cols)  # null-safe
        (
            DeltaTable.forName(spark, f"{fq_schema}.{name}").alias("t")
            .merge(src.alias("s"), f"t.{PRIMARY_KEY} = s.{PRIMARY_KEY}")
            .whenMatchedUpdateAll(condition=changed)
            .whenNotMatchedInsertAll()
            .whenNotMatchedBySourceDelete()
            .execute()
        )


def latest_version(spark, table):
    return spark.sql(f"DESCRIBE HISTORY {table} LIMIT 1").first()["version"]


def cdf_rows_since(spark, table, version):
    """Count data changes (inserts, update post-images, deletes) committed after `version`."""
    if latest_version(spark, table) <= version:
        return 0
    return (
        spark.read.option("readChangeFeed", "true")
        .option("startingVersion", version + 1)
        .table(table)
        .where("_change_type IN ('insert', 'update_postimage', 'delete')")
        .count()
    )


def ensure_index(client, endpoint_name, index_name, source_table, columns_to_sync=None):
    """Create a self-managed Delta Sync index on source_table if it doesn't exist, then wait until it's ready.

    Same call as managed embeddings, but with embedding_vector_column + embedding_dimension instead of
    embedding_source_column + embedding_model_endpoint_name.
    """
    try:
        client.get_index(endpoint_name=endpoint_name, index_name=index_name)
        print(f"{index_name} already exists")
    except Exception:
        client.create_delta_sync_index(
            endpoint_name=endpoint_name,
            index_name=index_name,
            source_table_name=source_table,
            primary_key=PRIMARY_KEY,
            pipeline_type="TRIGGERED",
            embedding_dimension=EMBEDDING_DIM,
            embedding_vector_column="body_vector",
            columns_to_sync=columns_to_sync,
        )
        print(f"created {index_name} on {source_table}")
    index = client.get_index(endpoint_name=endpoint_name, index_name=index_name)
    index.wait_until_ready(verbose=True, timeout=datetime.timedelta(hours=1))
    print(f"{index_name} ready")
    return index
