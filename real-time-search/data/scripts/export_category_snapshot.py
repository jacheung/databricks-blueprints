"""Export the workspace's `ai_classify` answers to `data/snapshots/` (committed), so rebuilds anywhere reuse them.

Run after catalog_prep has classified new rows (e.g. the full ~97K-query run), then commit the files. From this
folder's parent's parent (`real-time-search/`):

    uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I data/scripts/export_category_snapshot.py \\
        shared/configs/dev.yml

Exports every row: snapshot rows plus any the LLM classified since (catalog_prep's final tables hold both).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "src"), os.path.join(HERE, "..", "..", "shared", "src")]
from rts_data.sources import write_category_snapshot  # noqa: E402
from rts_shared.config import full_name, load_config, table  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

SNAPSHOTS = os.path.join(HERE, "..", "snapshots")
EXPORTS = {  # file -> (key, table short name or full-name suffix)
    "query_categories.csv.gz": ("query_id", "queries"),
    "product_title_categories.csv.gz": ("product_id", "product_title_categories"),
}


def fetch(w, warehouse, sql):
    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse, wait_timeout="50s")
    if r.status.error:
        raise RuntimeError(r.status.error)
    rows, chunk = list(r.result.data_array or []), r.result
    while chunk.next_chunk_index is not None:
        chunk = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk.next_chunk_index)
        rows += chunk.data_array or []
    return rows


def main(config_path):
    cfg = load_config(config_path)
    w = WorkspaceClient(profile=cfg["workspace"]["profile"])
    warehouse = next(iter(w.warehouses.list())).id  # any warehouse; serverless ones start on demand
    for name, (key, short) in EXPORTS.items():
        source = table(cfg, short) if short in cfg["tables"] else full_name(cfg, short)
        rows = fetch(w, warehouse, f"SELECT CAST({key} AS STRING), category FROM {source}")
        path = os.path.join(SNAPSHOTS, name)
        write_category_snapshot(path, key, rows)
        print(f"{name}: {len(rows):,} rows from {source}, {os.path.getsize(path) / 1e3:,.0f} KB")


if __name__ == "__main__":
    main(sys.argv[1])
