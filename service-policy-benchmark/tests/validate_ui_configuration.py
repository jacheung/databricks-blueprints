"""Validate Unity Gateway UI configuration before running the full benchmark."""

import json
import time

import requests
from databricks.sdk import WorkspaceClient

from run_policy_benchmark import (
    GATEWAY_PATH,
    REQUEST_TAG_HEADER,
    is_gateway_policy_block,
    load_config,
)


def get_json(session, url, headers):
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def verify_model_service(session, gateway_host, headers, config):
    model_service = config["model_service"]
    model_url = f"{gateway_host}/api/2.1/unity-catalog/model-services/{model_service['full_name']}"
    model = get_json(session, model_url, headers)
    destination_models = {
        destination.get("pay_per_token_config", {}).get("model")
        for destination in model.get("config", {}).get("routing", {}).get("destinations", [])
    }
    if model_service["destination_model"] not in destination_models:
        raise RuntimeError(
            f"{model_service['full_name']} does not route to "
            f"{model_service['destination_model']}."
        )


def verify_policy_probes(session, gateway_host, headers, config):
    preflight_config = config["preflight"]
    model_service = config["model_service"]["full_name"]
    unblocked_policies = []
    for probe in preflight_config["policy_probes"]:
        request_headers = {
            **headers,
            REQUEST_TAG_HEADER: json.dumps(
                {
                    "benchmark": "service_policy_preflight",
                    "probe_policy": probe["policy"],
                }
            ),
        }
        response = session.post(
            f"{gateway_host}{GATEWAY_PATH}",
            headers=request_headers,
            json={
                "model": model_service,
                "messages": [{"role": "user", "content": probe["prompt"]}],
                "max_tokens": preflight_config["max_tokens"],
            },
            timeout=120,
        )
        response_body = response.json()
        if not is_gateway_policy_block(response_body):
            response.raise_for_status()
            unblocked_policies.append(probe["policy"])

    if unblocked_policies:
        print(
            "Warning: policy probes were not blocked: "
            + ", ".join(unblocked_policies)
        )


def verify_payload_table(session, gateway_host, headers, config):
    table_name = config["audit_tables"]["inference_payload"]
    table_url = f"{gateway_host}/api/2.1/unity-catalog/tables/{table_name}"
    deadline = time.monotonic() + config["preflight"]["payload_table_wait_seconds"]
    while time.monotonic() < deadline:
        response = session.get(table_url, headers=headers, timeout=30)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            response.raise_for_status()
        time.sleep(10)
    raise RuntimeError(
        f"Inference payload table {table_name} was not found. Enable inference logging "
        "in the Unity Gateway UI before running the benchmark."
    )


def main():
    config = load_config()
    workspace_client = WorkspaceClient()
    session = requests.Session()
    headers = workspace_client.config.authenticate()
    gateway_host = workspace_client.config.host
    verify_model_service(session, gateway_host, headers, config)
    verify_policy_probes(session, gateway_host, headers, config)
    verify_payload_table(session, gateway_host, headers, config)
    print("Unity Gateway preflight validation passed.")


if __name__ == "__main__":
    main()
