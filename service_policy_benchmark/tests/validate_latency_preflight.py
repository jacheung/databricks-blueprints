"""Validate the ai_decide comparison setup before running the latency benchmark."""

import requests
from databricks.sdk import WorkspaceClient

from run_latency_comparison import ai_decide_call, make_post
from run_policy_benchmark import load_config
from validate_ui_configuration import get_json


def get_model_service(session, gateway_host, headers, full_name):
    return get_json(
        session, f"{gateway_host}/api/2.1/unity-catalog/model-services/{full_name}", headers
    )


def routing_destinations(model):
    return sorted(
        (
            destination.get("name"),
            destination.get("destination_type"),
            destination.get("pay_per_token_config", {}).get("model"),
            destination.get("traffic_percentage"),
        )
        for destination in model.get("config", {}).get("routing", {}).get("destinations", [])
        if not destination.get("is_deleted")
    )


def verify_service_parity(session, gateway_host, headers, config):
    guarded = config["model_service"]["full_name"]
    unguarded = config["latency_comparison"]["ai_decide_model_service"]
    guarded_model = get_model_service(session, gateway_host, headers, guarded)
    unguarded_model = get_model_service(session, gateway_host, headers, unguarded)
    if routing_destinations(guarded_model) != routing_destinations(unguarded_model):
        raise RuntimeError(
            f"Routing differs: {guarded}={routing_destinations(guarded_model)} vs "
            f"{unguarded}={routing_destinations(unguarded_model)}."
        )
    active_policies = [
        policy["name"]
        for policy in unguarded_model["config"].get("service_policies", [])
        if not policy.get("is_deleted")
    ]
    if active_policies:
        raise RuntimeError(f"{unguarded} must have no service policies; found {active_policies}.")
    inference_table = unguarded_model["config"].get("inference_table")
    if not inference_table or inference_table.get("disabled") or inference_table.get("is_deleted"):
        raise RuntimeError(f"{unguarded} must have inference logging enabled like {guarded}.")


def verify_ai_decide(post, config):
    latency_config = config["latency_comparison"]
    probe = next(
        probe for probe in config["preflight"]["policy_probes"]
        if probe["policy"] == "block_jailbreak"
    )
    call = ai_decide_call(
        post,
        latency_config,
        {"user_prompt": probe["prompt"]},
        latency_config["input_questions"],
        "input_check",
    )
    if call["status_code"] != 200 or set(call.get("probabilities", {})) != set(
        latency_config["input_questions"]
    ):
        raise RuntimeError(f"ai_decide probe failed ({call['status_code']}): {call['body']}")
    print(f"ai_decide probe: {call['probabilities']} in {call['duration_ms']:.0f} ms")


def main():
    config = load_config()
    latency_config = config["latency_comparison"]
    workspace_client = WorkspaceClient()
    session = requests.Session()
    headers = workspace_client.config.authenticate()
    gateway_host = workspace_client.config.host
    verify_service_parity(session, gateway_host, headers, config)
    post = make_post(
        session, gateway_host, workspace_client.config.authenticate,
        latency_config["request_timeout_seconds"],
    )
    verify_ai_decide(post, config)
    print("ai_decide latency preflight validation passed.")


if __name__ == "__main__":
    main()
