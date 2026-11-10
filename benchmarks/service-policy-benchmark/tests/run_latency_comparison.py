"""Compare end-to-end guardrail latency: Unity Gateway service policies vs. ai_decide."""

import argparse
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from databricks.sdk import WorkspaceClient
from pyspark.sql.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from generate_policy_benchmark import build_rows
from run_policy_benchmark import (
    GATEWAY_PATH,
    REQUEST_TAG_HEADER,
    append_rows,
    is_gateway_policy_block,
    load_config,
    response_text,
)


ARMS = ("service_policies", "ai_decide_packed", "ai_decide_split", "no_guardrails")
POLICY_PHASES = {"pre_call": "input", "post_call": "output"}
RESULT_SCHEMA = StructType(
    [
        StructField("latency_run_id", StringType(), False),
        StructField("corpus_version_id", StringType(), False),
        StructField("seed", IntegerType(), False),
        StructField("rep", IntegerType(), False),
        StructField("arm", StringType(), False),
        StructField("arm_position", IntegerType(), False),
        StructField("query_id", StringType(), False),
        StructField("policy", StringType(), False),
        StructField("phase", StringType(), False),
        StructField("expected_result", StringType(), False),
        StructField("observed_result", StringType(), False),
        StructField("passed", BooleanType(), True),
        StructField("blocked_by", StringType(), True),
        StructField("total_ms", DoubleType(), False),
        StructField("input_check_ms", DoubleType(), True),
        StructField("model_ms", DoubleType(), True),
        StructField("output_check_ms", DoubleType(), True),
        StructField("model_status_code", IntegerType(), True),
        StructField("prompt_tokens", IntegerType(), True),
        StructField("completion_tokens", IntegerType(), True),
        StructField("reasoning_tokens", IntegerType(), True),
        StructField("total_tokens", IntegerType(), True),
        StructField("finish_reason", StringType(), True),
        StructField("response_text", StringType(), True),
        StructField("started_at", TimestampType(), False),
        StructField("ended_at", TimestampType(), False),
    ]
)
STAGE_SCHEMA = StructType(
    [
        StructField("latency_run_id", StringType(), False),
        StructField("rep", IntegerType(), False),
        StructField("arm", StringType(), False),
        StructField("query_id", StringType(), False),
        StructField("stage", StringType(), False),
        StructField("question_ids", StringType(), True),
        StructField("endpoint", StringType(), False),
        StructField("status_code", IntegerType(), True),
        StructField("duration_ms", DoubleType(), False),
        StructField("request_bytes", IntegerType(), False),
        StructField("response_bytes", IntegerType(), True),
        StructField("request_id", StringType(), True),
        StructField("probabilities", MapType(StringType(), DoubleType()), True),
        StructField("flagged", StringType(), True),
        StructField("ai_decide_version", StringType(), True),
        StructField("timed_out", BooleanType(), False),
        StructField("error", StringType(), True),
        StructField("response", StringType(), True),
    ]
)


def elapsed_ms(start):
    return (time.perf_counter() - start) * 1000


def make_post(session, host, auth_headers, timeout):
    """Return post(path, body, tags=None) -> timed call record."""

    def post(path, body, tags=None):
        headers = auth_headers()
        if tags is not None:
            headers = {**headers, REQUEST_TAG_HEADER: json.dumps(tags)}
        call = {
            "endpoint": path,
            "request_bytes": len(json.dumps(body).encode()),
            "status_code": None,
            "response_bytes": None,
            "request_id": None,
            "timed_out": False,
            "error": None,
            "body": {},
        }
        start = time.perf_counter()
        try:
            response = session.post(f"{host}{path}", headers=headers, json=body, timeout=timeout)
        except requests.Timeout as error:
            call.update(duration_ms=elapsed_ms(start), timed_out=True, error=str(error))
            return call
        except requests.RequestException as error:
            call.update(duration_ms=elapsed_ms(start), error=str(error))
            return call
        call["duration_ms"] = elapsed_ms(start)
        try:
            call["body"] = response.json()
        except ValueError:
            call["body"] = {"raw_response": response.text}
        call.update(
            status_code=response.status_code,
            response_bytes=len(response.content),
            request_id=response.headers.get("x-request-id"),
        )
        return call

    return post


def failure_result(call):
    if call["timed_out"]:
        return "TIMEOUT"
    if call["error"] is not None or call["status_code"] >= 400:
        return "ERROR"
    return None


def ai_decide_body(state, questions):
    return {
        "state": state,
        "questions": {
            question_id: {
                "type": "noul",
                "instructions": question["instructions"],
                "criteria": question["criteria"],
            }
            for question_id, question in questions.items()
        },
    }


def flagged(probabilities, threshold):
    return sorted(
        question_id for question_id, probability in probabilities.items() if probability >= threshold
    )


def ai_decide_call(post, latency_config, state, questions, stage):
    call = post(latency_config["ai_decide_path"], ai_decide_body(state, questions))
    call.update(stage=stage, question_ids=",".join(sorted(questions)))
    if failure_result(call) is None:
        answers = call["body"].get("response", {}).get("answers", {})
        call["probabilities"] = {
            question_id: float(answer["probability"]) for question_id, answer in answers.items()
        }
        call["flagged"] = flagged(call["probabilities"], latency_config["threshold"])
        call["ai_decide_version"] = call["body"].get("metadata", {}).get("version")
    return call


def input_checks(post, arm, row, latency_config):
    questions = latency_config["input_questions"]
    state = {"user_prompt": row["prompt"]}
    if arm == "ai_decide_packed":
        return [ai_decide_call(post, latency_config, state, questions, "input_check")]
    with ThreadPoolExecutor(max_workers=len(questions)) as pool:
        return list(
            pool.map(
                lambda question_id: ai_decide_call(
                    post, latency_config, state, {question_id: questions[question_id]}, "input_check"
                ),
                questions,
            )
        )


def model_call(post, model_service, row, config, tags):
    call = post(
        GATEWAY_PATH,
        {
            "model": model_service,
            "messages": [{"role": "user", "content": row["prompt"]}],
            "max_tokens": config["latency_comparison"]["max_tokens"],
        },
        tags,
    )
    call.update(stage="model", question_ids=None)
    return call


def run_arm(arm, row, post, config, tags):
    """Run one corpus row through one arm; return (result, stage calls)."""
    latency_config = config["latency_comparison"]
    result = {
        "observed_result": None,
        "blocked_by": None,
        "input_check_ms": None,
        "model_ms": None,
        "output_check_ms": None,
        "model_status_code": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "finish_reason": None,
        "response_text": None,
    }
    calls = []
    start = time.perf_counter()

    def finish(observed_result, blocked_by=None):
        result.update(
            observed_result=observed_result,
            blocked_by=blocked_by,
            total_ms=elapsed_ms(start),
            passed=observed_result == row["expected_result"]
            if observed_result in ("BLOCK", "ALLOW")
            else None,
        )
        return result, calls

    def check_stage(stage_calls, label):
        calls.extend(stage_calls)
        for call in stage_calls:
            failure = failure_result(call)
            if failure:
                return finish(failure)
        hits = sorted(question_id for call in stage_calls for question_id in call["flagged"])
        if hits:
            return finish("BLOCK", f"{label}:{','.join(hits)}")
        return None

    if arm in ("ai_decide_packed", "ai_decide_split"):
        stage_start = time.perf_counter()
        stage_calls = input_checks(post, arm, row, latency_config)
        result["input_check_ms"] = elapsed_ms(stage_start)
        outcome = check_stage(stage_calls, "input")
        if outcome:
            return outcome

    model_service = (
        config["model_service"]["full_name"]
        if arm == "service_policies"
        else latency_config["ai_decide_model_service"]
    )
    call = model_call(post, model_service, row, config, tags)
    calls.append(call)
    result["model_ms"] = call["duration_ms"]
    result["model_status_code"] = call["status_code"]
    if arm == "service_policies" and is_gateway_policy_block(call["body"]):
        policy = call["body"]["databricks_service_policy"]
        return finish("BLOCK", f"{POLICY_PHASES[policy['phase']]}:{policy['name']}")
    failure = failure_result(call)
    if failure:
        return finish(failure)
    usage = call["body"].get("usage") or {}
    result.update(
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
        total_tokens=usage.get("total_tokens"),
        finish_reason=(call["body"].get("choices") or [{}])[0].get("finish_reason"),
        response_text=response_text(call["body"]),
    )
    if arm in ("service_policies", "no_guardrails"):
        return finish("ALLOW")

    stage_start = time.perf_counter()
    output_call = ai_decide_call(
        post,
        latency_config,
        {"user_prompt": row["prompt"], "model_response": result["response_text"]},
        latency_config["output_questions"],
        "output_check",
    )
    result["output_check_ms"] = elapsed_ms(stage_start)
    outcome = check_stage([output_call], "output")
    if outcome:
        return outcome
    return finish("ALLOW")


def arm_order(seed, rep, query_id):
    arms = list(ARMS)
    random.Random(f"{seed}:{rep}:{query_id}").shuffle(arms)
    return arms


def stage_row(latency_run_id, rep, arm, query_id, call):
    return {
        "latency_run_id": latency_run_id,
        "rep": rep,
        "arm": arm,
        "query_id": query_id,
        "stage": call["stage"],
        "question_ids": call["question_ids"],
        "endpoint": call["endpoint"],
        "status_code": call["status_code"],
        "duration_ms": call["duration_ms"],
        "request_bytes": call["request_bytes"],
        "response_bytes": call["response_bytes"],
        "request_id": call["request_id"],
        "probabilities": call.get("probabilities"),
        "flagged": ",".join(call["flagged"]) if "flagged" in call else None,
        "ai_decide_version": call.get("ai_decide_version"),
        "timed_out": call["timed_out"],
        "error": call["error"],
        "response": json.dumps(call["body"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--latency-run-id", required=True)
    parser.add_argument("--corpus-version-id", required=True)
    parser.add_argument("--repetitions", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--max-queries", type=int, default=0)
    arguments = parser.parse_args()
    spark = globals()["spark"]
    config = load_config()
    latency_config = config["latency_comparison"]
    benchmark_rows = build_rows(
        blocked_cases_per_policy=config["benchmark"]["blocked_cases_per_policy"],
        allowed_cases_per_policy=config["benchmark"]["allowed_cases_per_policy"],
    )
    if arguments.max_queries:
        random.Random(arguments.seed).shuffle(benchmark_rows)
        benchmark_rows = benchmark_rows[: arguments.max_queries]

    workspace_client = WorkspaceClient()
    post = make_post(
        requests.Session(),
        workspace_client.config.host,
        workspace_client.config.authenticate,
        latency_config["request_timeout_seconds"],
    )
    # Rep 0 is warmup: it is persisted for inspection and excluded from the summary.
    schedule = [(0, benchmark_rows[: latency_config["warmup_requests"]])] + [
        (rep, benchmark_rows) for rep in range(1, arguments.repetitions + 1)
    ]
    for rep, rep_rows in schedule:
        results, stage_rows = [], []
        for row in rep_rows:
            for position, arm in enumerate(arm_order(arguments.seed, rep, row["query_id"])):
                tags = {
                    "benchmark": latency_config["request_tags"]["benchmark"],
                    "latency_run_id": arguments.latency_run_id,
                    "arm": arm,
                    "rep": str(rep),
                    config["benchmark"]["request_tags"]["query_id_key"]: row["query_id"],
                }
                started_at = datetime.now(timezone.utc)
                result, calls = run_arm(arm, row, post, config, tags)
                results.append(
                    {
                        "latency_run_id": arguments.latency_run_id,
                        "corpus_version_id": arguments.corpus_version_id,
                        "seed": arguments.seed,
                        "rep": rep,
                        "arm": arm,
                        "arm_position": position,
                        "query_id": row["query_id"],
                        "policy": row["policy"],
                        "phase": row["phase"],
                        "expected_result": row["expected_result"],
                        **result,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc),
                    }
                )
                stage_rows.extend(
                    stage_row(arguments.latency_run_id, rep, arm, row["query_id"], call)
                    for call in calls
                )
        append_rows(spark, latency_config["tables"]["results"], results, RESULT_SCHEMA)
        append_rows(spark, latency_config["tables"]["stage_calls"], stage_rows, STAGE_SCHEMA)
        print(f"Rep {rep}: wrote {len(results)} results and {len(stage_rows)} stage calls.")


if __name__ == "__main__":
    main()
