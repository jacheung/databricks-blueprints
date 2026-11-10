"""Print latency, overhead, and accuracy summaries for one latency comparison run."""

import argparse

from run_policy_benchmark import load_config


def percentiles(column):
    return ", ".join(
        f"round(percentile({column}, {quantile}), 1) AS p{int(quantile * 100)}_{column}"
        for quantile in (0.5, 0.9, 0.95, 0.99)
    )


def summary_queries(results, stage_calls, run_id):
    scored = f"""
      SELECT *,
        CASE WHEN phase = 'ON_CALL' AND expected_result = 'BLOCK'
          THEN 'input_block_expected' ELSE 'reaches_model_expected' END AS bucket,
        coalesce(blocked_by, '') NOT LIKE 'input:%' AS reached_model
      FROM {results}
      WHERE latency_run_id = '{run_id}' AND rep > 0
    """
    return {
        "End-to-end latency per arm (ms)": f"""
          SELECT arm, count(*) AS requests,
            sum(CAST(observed_result = 'ERROR' AS INT)) AS errors,
            sum(CAST(observed_result = 'TIMEOUT' AS INT)) AS timeouts,
            round(avg(total_ms), 1) AS mean_total_ms, {percentiles('total_ms')}
          FROM ({scored}) WHERE observed_result IN ('BLOCK', 'ALLOW')
          GROUP BY arm ORDER BY arm
        """,
        "End-to-end latency per arm and bucket (ms)": f"""
          SELECT arm, bucket, count(*) AS requests,
            round(avg(total_ms), 1) AS mean_total_ms, {percentiles('total_ms')}
          FROM ({scored}) WHERE observed_result IN ('BLOCK', 'ALLOW')
          GROUP BY arm, bucket ORDER BY bucket, arm
        """,
        "Stage breakdown per arm (ms)": f"""
          SELECT arm,
            round(percentile(input_check_ms, 0.5), 1) AS p50_input_check_ms,
            round(percentile(input_check_ms, 0.95), 1) AS p95_input_check_ms,
            round(percentile(model_ms, 0.5), 1) AS p50_model_ms,
            round(percentile(model_ms, 0.95), 1) AS p95_model_ms,
            round(percentile(output_check_ms, 0.5), 1) AS p50_output_check_ms,
            round(percentile(output_check_ms, 0.95), 1) AS p95_output_check_ms,
            round(avg(total_tokens), 1) AS mean_total_tokens,
            round(avg(reasoning_tokens), 1) AS mean_reasoning_tokens,
            round(100 * avg(CAST(finish_reason = 'length' AS INT)), 1) AS pct_hit_max_tokens
          FROM ({scored}) WHERE observed_result IN ('BLOCK', 'ALLOW')
          GROUP BY arm ORDER BY arm
        """,
        "Guardrail overhead vs. no_guardrails for requests that reached the model (ms)": f"""
          WITH scored AS ({scored}),
          baseline AS (SELECT rep, query_id, total_ms FROM scored WHERE arm = 'no_guardrails'
                       AND observed_result = 'ALLOW')
          SELECT s.arm, s.bucket, count(*) AS pairs,
            round(percentile(s.total_ms - b.total_ms, 0.5), 1) AS p50_overhead_ms,
            round(percentile(s.total_ms - b.total_ms, 0.95), 1) AS p95_overhead_ms
          FROM scored s JOIN baseline b USING (rep, query_id)
          WHERE s.arm != 'no_guardrails' AND s.observed_result IN ('BLOCK', 'ALLOW')
            AND s.reached_model
          GROUP BY s.arm, s.bucket ORDER BY s.bucket, s.arm
        """,
        "ai_decide call latency (ms)": f"""
          SELECT arm, stage, question_ids, count(*) AS calls,
            sum(CAST(status_code != 200 OR status_code IS NULL AS INT)) AS failures,
            round(avg(duration_ms), 1) AS mean_duration_ms, {percentiles('duration_ms')}
          FROM {stage_calls}
          WHERE latency_run_id = '{run_id}' AND rep > 0 AND stage != 'model'
          GROUP BY arm, stage, question_ids ORDER BY stage, arm, question_ids
        """,
        "Accuracy per arm and policy (BLOCK is positive)": f"""
          SELECT arm, policy,
            sum(CAST(expected_result = 'BLOCK' AND observed_result = 'BLOCK' AS INT)) AS tp,
            sum(CAST(expected_result = 'ALLOW' AND observed_result = 'BLOCK' AS INT)) AS fp,
            sum(CAST(expected_result = 'BLOCK' AND observed_result = 'ALLOW' AS INT)) AS fn,
            sum(CAST(expected_result = 'ALLOW' AND observed_result = 'ALLOW' AS INT)) AS tn,
            round(100 * avg(CAST(passed AS INT)), 2) AS accuracy_pct
          FROM ({scored})
          WHERE arm != 'no_guardrails' AND passed IS NOT NULL
          GROUP BY arm, policy ORDER BY policy, arm
        """,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--latency-run-id", required=True)
    arguments = parser.parse_args()
    spark = globals()["spark"]
    tables = load_config()["latency_comparison"]["tables"]
    for title, query in summary_queries(
        tables["results"], tables["stage_calls"], arguments.latency_run_id
    ).items():
        print(f"\n## {title}")
        spark.sql(query).show(100, truncate=False)


if __name__ == "__main__":
    main()
