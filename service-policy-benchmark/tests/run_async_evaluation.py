"""Assess delayed AI Gateway audit records for one benchmark run."""

import argparse

from run_policy_benchmark import load_config


def sql_literal(value):
    return value.replace("'", "''")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark-run-id", required=True)
    arguments = parser.parse_args()
    spark = globals()["spark"]
    config = load_config()
    tables = config["audit_tables"]
    run_id = sql_literal(arguments.benchmark_run_id)
    run_rows = spark.sql(
        f"SELECT corpus_version_id FROM {tables['benchmark_runs']} "
        f"WHERE benchmark_run_id = '{run_id}'"
    ).collect()
    if len(run_rows) != 1:
        raise ValueError(f"Expected one benchmark run for {arguments.benchmark_run_id}.")
    corpus_version_id = sql_literal(run_rows[0].corpus_version_id)
    assessment = spark.sql(
        f"""
        WITH corpus AS (
          SELECT query_id, policy, phase, expected_result
          FROM {tables['corpus']}
          WHERE corpus_version_id = '{corpus_version_id}'
        ),
        usage AS (
          SELECT request_tags['benchmark_query_id'] AS query_id, request_id, total_tokens,
                 ROW_NUMBER() OVER (
                   PARTITION BY request_tags['benchmark_query_id'] ORDER BY event_time DESC
                 ) AS row_number
          FROM {tables['usage']}
          WHERE service_name = '{config['model_service']['full_name']}'
            AND request_tags['benchmark'] = '{config['benchmark']['request_tags']['benchmark']}'
            AND request_tags['benchmark_run_id'] = '{run_id}'
        ),
        payload AS (
          SELECT request_tags['benchmark_query_id'] AS query_id, request_id, status_code,
                 ROW_NUMBER() OVER (
                   PARTITION BY request_tags['benchmark_query_id'] ORDER BY event_time DESC
                 ) AS row_number
          FROM {tables['inference_payload']}
          WHERE request_tags['benchmark'] = '{config['benchmark']['request_tags']['benchmark']}'
            AND request_tags['benchmark_run_id'] = '{run_id}'
        )
        SELECT
          '{run_id}' AS benchmark_run_id,
          corpus.query_id,
          corpus.policy,
          corpus.phase,
          corpus.expected_result,
          CASE
            WHEN corpus.phase = 'ON_CALL' AND usage.request_id IS NULL THEN 'PENDING'
            WHEN corpus.phase = 'ON_CALL' AND COALESCE(usage.total_tokens, 0) = 0 THEN 'BLOCK'
            WHEN corpus.phase = 'ON_CALL' THEN 'ALLOW'
            WHEN payload.request_id IS NULL THEN 'PENDING'
            WHEN payload.status_code = 400 THEN 'BLOCK'
            ELSE 'ALLOW'
          END AS observed_result
        FROM corpus
        LEFT JOIN usage ON corpus.query_id = usage.query_id AND usage.row_number = 1
        LEFT JOIN payload ON corpus.query_id = payload.query_id AND payload.row_number = 1
        """
    )
    results_table = tables["async_results"]
    if spark.catalog.tableExists(results_table):
        spark.sql(f"DELETE FROM {results_table} WHERE benchmark_run_id = '{run_id}'")
    assessment.write.mode("append").option("mergeSchema", "true").saveAsTable(results_table)
    assessment.groupBy("policy", "phase", "expected_result", "observed_result").count().show(
        truncate=False
    )


if __name__ == "__main__":
    main()
