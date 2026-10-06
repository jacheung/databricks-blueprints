"""Materialize and execute the Unity Gateway service-policy benchmark."""

import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from databricks.sdk import WorkspaceClient
from pyspark.sql.types import (
    BooleanType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from generate_policy_benchmark import build_rows


GATEWAY_PATH = "/ai-gateway/mlflow/v1/chat/completions"
REQUEST_TAG_HEADER = "Databricks-Ai-Gateway-Request-Tags"
CORPUS_SCHEMA = StructType(
    [
        StructField("corpus_version_id", StringType(), False),
        StructField("query_id", StringType(), False),
        StructField("policy", StringType(), False),
        StructField("phase", StringType(), False),
        StructField("sensitive_data_classification", StringType(), False),
        StructField("prompt", StringType(), False),
        StructField("expected_result", StringType(), False),
        StructField("model_service", StringType(), False),
        StructField("created_at", TimestampType(), False),
    ]
)
RUN_SCHEMA = StructType(
    [
        StructField("benchmark_run_id", StringType(), False),
        StructField("corpus_version_id", StringType(), False),
        StructField("model_service", StringType(), False),
        StructField("expected_query_count", IntegerType(), False),
        StructField("started_at", TimestampType(), False),
    ]
)
RESULT_SCHEMA = StructType(
    [
        StructField("benchmark_run_id", StringType(), False),
        StructField("query_id", StringType(), False),
        StructField("observed_result", StringType(), False),
        StructField("passed", BooleanType(), True),
        StructField("status_code", IntegerType(), True),
        StructField("response", StringType(), True),
        StructField("response_text", StringType(), True),
        StructField("executed_at", TimestampType(), False),
    ]
)


def response_text(response_body):
    choices = response_body.get("choices", [])
    if not choices:
        return ""
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content)


def is_gateway_policy_block(response_body):
    return "databricks_service_policy" in response_body


def load_config():
    config_path = Path(load_config.__code__.co_filename).resolve().parent.parent / "config.yml"
    with config_path.open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def execute_query(session, headers, gateway_host, config, row, benchmark_run_id):
    benchmark_config = config["benchmark"]
    request_body = {
        "model": config["model_service"]["full_name"],
        "messages": [{"role": "user", "content": row["prompt"]}],
        "max_tokens": benchmark_config["max_tokens"],
    }
    request_time = datetime.now(timezone.utc)
    request_headers = {
        **headers,
        REQUEST_TAG_HEADER: json.dumps(
            {
                benchmark_config["request_tags"]["query_id_key"]: row["query_id"],
                "benchmark": benchmark_config["request_tags"]["benchmark"],
                "benchmark_run_id": benchmark_run_id,
            }
        ),
    }
    try:
        response = session.post(
            f"{gateway_host}{GATEWAY_PATH}",
            headers=request_headers,
            json=request_body,
            timeout=120,
        )
    except requests.RequestException as error:
        return {
            "query_id": row["query_id"],
            "observed_result": "TIMEOUT",
            "passed": None,
            "status_code": None,
            "response": json.dumps({"error": str(error)}),
            "response_text": "",
            "executed_at": request_time,
        }
    try:
        response_body = response.json()
    except ValueError:
        response_body = {"raw_response": response.text}

    if is_gateway_policy_block(response_body):
        observed_result = "BLOCK"
    elif response.status_code >= 400:
        observed_result = "ERROR"
    else:
        observed_result = "ALLOW"

    return {
        "query_id": row["query_id"],
        "observed_result": observed_result,
        "passed": observed_result == row["expected_result"],
        "status_code": response.status_code,
        "response": json.dumps(response_body),
        "response_text": response_text(response_body),
        "executed_at": request_time,
    }


def table_exists(spark, table_name):
    return spark.catalog.tableExists(table_name)


def append_rows(spark, table_name, rows, schema):
    spark.createDataFrame(rows, schema=schema).write.mode("append").option(
        "mergeSchema", "true"
    ).saveAsTable(table_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-run-id", required=True)
    parser.add_argument("--corpus-version-id", required=True)
    arguments = parser.parse_args()
    spark = globals()["spark"]
    config = load_config()
    benchmark_config = config["benchmark"]
    model_service = config["model_service"]["full_name"]
    corpus_table = config["audit_tables"]["corpus"]
    benchmark_rows = build_rows(
        blocked_cases_per_policy=benchmark_config["blocked_cases_per_policy"],
        allowed_cases_per_policy=benchmark_config["allowed_cases_per_policy"],
    )
    if len(benchmark_rows) != benchmark_config["expected_total_queries"]:
        raise ValueError(
            f"Generated {len(benchmark_rows)} rows; expected "
            f"{benchmark_config['expected_total_queries']}."
        )
    created_at = datetime.now(timezone.utc)
    initial_rows = [
        {
            **row,
            "corpus_version_id": arguments.corpus_version_id,
            "model_service": model_service,
            "created_at": created_at,
        }
        for row in benchmark_rows
    ]
    if table_exists(spark, corpus_table) and "corpus_version_id" not in spark.table(
        corpus_table
    ).columns:
        spark.sql(f"ALTER TABLE {corpus_table} ADD COLUMNS (corpus_version_id STRING)")
    if not table_exists(spark, corpus_table) or not spark.table(corpus_table).where(
        f"corpus_version_id = '{arguments.corpus_version_id}'"
    ).limit(1).count():
        append_rows(spark, corpus_table, initial_rows, CORPUS_SCHEMA)

    runs_table = config["audit_tables"]["benchmark_runs"]
    results_table = config["audit_tables"]["benchmark_results"]
    if not table_exists(spark, runs_table):
        append_rows(
            spark,
            runs_table,
            [{"benchmark_run_id": arguments.benchmark_run_id, "corpus_version_id": arguments.corpus_version_id, "model_service": model_service, "expected_query_count": len(benchmark_rows), "started_at": created_at}],
            RUN_SCHEMA,
        )
    elif not spark.table(runs_table).where(
        f"benchmark_run_id = '{arguments.benchmark_run_id}'"
    ).limit(1).count():
        append_rows(spark, runs_table, [{"benchmark_run_id": arguments.benchmark_run_id, "corpus_version_id": arguments.corpus_version_id, "model_service": model_service, "expected_query_count": len(benchmark_rows), "started_at": created_at}], RUN_SCHEMA)

    workspace_client = WorkspaceClient()
    session = requests.Session()
    existing_query_ids = set()
    if table_exists(spark, results_table):
        existing_query_ids = {row.query_id for row in spark.table(results_table).where(f"benchmark_run_id = '{arguments.benchmark_run_id}'").select("query_id").collect()}
    for row in benchmark_rows:
        if row["query_id"] in existing_query_ids:
            continue
        result = execute_query(session, workspace_client.config.authenticate(), workspace_client.config.host, config, row, arguments.benchmark_run_id)
        append_rows(spark, results_table, [{"benchmark_run_id": arguments.benchmark_run_id, **result}], RESULT_SCHEMA)


if __name__ == "__main__":
    main()
