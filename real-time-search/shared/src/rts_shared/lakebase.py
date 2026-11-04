"""Lakebase helpers shared by the notebooks that publish Delta tables to the shared instance (QAC prefixes, demo
scenarios). Synced tables are snapshots: each run re-copies the overwritten Delta table in full."""

from __future__ import annotations

import time


def branch(cfg: dict) -> str:
    lb = cfg["lakebase"]
    return f"projects/{lb['instance']}/branches/{lb['branch']}"


def _wait_online(w, synced_name: str, timeout_s: int):
    deadline, state = time.time() + timeout_s, None
    while time.time() < deadline:
        st = w.postgres.get_synced_table(name=synced_name).status
        state = str(st.detailed_state)
        if "FAILED" in state:
            raise RuntimeError(f"synced table {state}: {st.message}")
        if "ONLINE" in state and "UPDATING" not in state:
            return st
        time.sleep(20)
    raise TimeoutError(f"synced table not online after {timeout_s}s: {state}")


def sync_snapshot(w, cfg: dict, source: str, target: str, primary_key: list[str], timeout_s: int = 1800):
    """Create the snapshot synced table `target` (catalog.schema.table) from Delta `source`, or re-snapshot it."""
    from databricks.sdk.errors import NotFound
    from databricks.sdk.service.postgres import (NewPipelineSpec, SyncedTable, SyncedTableSyncedTableSpec,
                                                 SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy)

    uc, synced_name = cfg["unity_catalog"], f"synced_tables/{target}"
    try:
        existing = w.postgres.get_synced_table(name=synced_name)
    except NotFound:
        existing = None
    if existing is None:
        w.postgres.create_synced_table(
            synced_table_id=target,
            synced_table=SyncedTable(spec=SyncedTableSyncedTableSpec(
                source_table_full_name=source, primary_key_columns=primary_key,
                scheduling_policy=SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy.SNAPSHOT,
                branch=branch(cfg), postgres_database=cfg["lakebase"]["database"],
                create_database_objects_if_missing=True,
                new_pipeline_spec=NewPipelineSpec(storage_catalog=uc["catalog"], storage_schema=uc["schema"]))))
        return _wait_online(w, synced_name, timeout_s)
    pipeline_id = existing.status.pipeline_id
    update_id = w.pipelines.start_update(pipeline_id=pipeline_id).update_id
    deadline = time.time() + timeout_s
    while (state := str(w.pipelines.get_update(pipeline_id=pipeline_id, update_id=update_id).update.state)) \
            .split(".")[-1] not in ("COMPLETED", "FAILED", "CANCELED"):
        if time.time() > deadline:
            raise TimeoutError(f"snapshot refresh still {state}")
        time.sleep(20)
    if not state.endswith("COMPLETED"):
        raise RuntimeError(f"snapshot refresh {state}")
    return _wait_online(w, synced_name, timeout_s)


def features_database(cfg: dict) -> str:
    """The Postgres database holding the Feature Store online tables. The online store creates it on the shared
    instance, named after the Unity Catalog catalog (`main`); synced tables live in `lakebase.database` instead."""
    return cfg["unity_catalog"]["catalog"]


def connect(w, cfg: dict, database: str | None = None):
    """pg8000 connection as the current identity, to `database` (default `lakebase.database`). Pure Python: native
    libpq drivers can abort Python on serverless."""
    import ssl

    import pg8000.native

    endpoint = next(e for e in w.postgres.list_endpoints(parent=branch(cfg))
                    if "READ_WRITE" in str(e.status.endpoint_type))
    token = w.postgres.generate_database_credential(endpoint=endpoint.name).token
    return pg8000.native.Connection(user=w.current_user.me().user_name, host=endpoint.status.hosts.host,
                                    database=database or cfg["lakebase"]["database"], password=token,
                                    ssl_context=ssl.create_default_context())
