# Unity Gateway weighted model-service demo

This folder contains a Databricks source notebook that creates and queries a
Unity Gateway model service. The service routes primary traffic 50/50 between
one Databricks-hosted OpenAI Foundation Model API model and one Databricks-hosted
Llama Foundation Model API model.

## Contents

| File | Purpose |
| --- | --- |
| `01_unity_gateway_weighted_model_service.py` | Creates the service with the Databricks Python SDK, previews its payload, calls it twice, and displays the OpenAI-compatible responses. |

## Prerequisites

- A workspace with Unity Gateway model services enabled.
- `USE CATALOG` and `USE SCHEMA` on the chosen parent schema.
- Permission to create a model service in that schema.
- `EXECUTE` on both selected Databricks Foundation Model API resources.
- Foundation Model API models available in the workspace: one OpenAI model and one Llama model.

## Run

1. Import or open `01_unity_gateway_weighted_model_service.py` in a Databricks notebook.
2. Attach supported compute that includes the Databricks SDK and `requests`.
3. Run the **Discover available Foundation Model API resources** cell.
4. Set `OPENAI_MODEL` and `LLAMA_MODEL` to two resources displayed by that cell.
5. Set a catalog, schema, and unique `MODEL_SERVICE_ID`.
6. Review the printed create payload, then set `CREATE_SERVICE` to `true`.
7. Run the creation and invocation cells.

## What the notebook proves

- **Programmatic setup:** `WorkspaceClient().ai_gateway.create_model_service(...)` creates the model service.
- **Weighted routing:** the two primary destinations each receive `traffic_percentage: 50`.
- **Stable application contract:** callers address the logical Unity Catalog model-service name through `/ai-gateway/mlflow/v1/chat/completions` and send one OpenAI-compatible request payload.
- **Unified output shape:** both calls return chat-completions payloads with `choices` and `usage`, regardless of which weighted destination handles them.

## Routing expectation

The notebook makes two requests because that is easy to demonstrate, but it
does **not** guarantee one OpenAI and one Llama result. With independent 50/50
routing, the chance of two calls using different destinations is 50%.

The response identifies the logical model service, which is intentionally
decoupled from the backing model. Inspect Unity Gateway usage or inference logs
for authoritative per-request destination attribution; do not infer the route
from the generated text.

## Safe reruns

`CREATE_SERVICE` defaults to `false`, so opening the notebook only previews
payloads. Once enabled, a rerun reuses a service with the same name rather than
replacing its configuration. Use a new `MODEL_SERVICE_ID` for a fresh demo or
explicitly update/delete the existing service through the SDK or Unity Gateway.
