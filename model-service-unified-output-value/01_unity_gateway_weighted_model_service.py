# Databricks notebook source
# MAGIC %md
# MAGIC # Unity Gateway model service: 50/50 OpenAI and Llama routing
# MAGIC
# MAGIC This notebook programmatically creates a Unity Gateway model service with two
# MAGIC Databricks Foundation Model API destinations:
# MAGIC
# MAGIC - OpenAI model: 50% of primary traffic
# MAGIC - Llama model: 50% of primary traffic
# MAGIC
# MAGIC It then calls the model service twice through the single OpenAI-compatible
# MAGIC Unity Gateway API and prints the request and response payloads.
# MAGIC
# MAGIC > **Important:** two independent requests are not guaranteed to select different
# MAGIC > destinations. With 50/50 routing there is a 50% chance that two requests use
# MAGIC > the same backing model. The response model field identifies the logical model
# MAGIC > service, not necessarily the selected backing destination.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC
# MAGIC Set `CREATE_SERVICE` to `true` only after choosing model resource names that
# MAGIC exist in this workspace. Run the discovery cell first if the defaults are not
# MAGIC available.

# COMMAND ----------

dbutils.widgets.text("CATALOG", "main", "Unity Catalog catalog")
dbutils.widgets.text("SCHEMA", "default", "Unity Catalog schema")
dbutils.widgets.text(
    "MODEL_SERVICE_ID",
    "openai_llama_5050_demo",
    "Model service identifier",
)
dbutils.widgets.text(
    "OPENAI_MODEL",
    "models/system.ai.databricks-gpt-5",
    "OpenAI Foundation Model API resource",
)
dbutils.widgets.text(
    "LLAMA_MODEL",
    "models/system.ai.databricks-llama-4-maverick",
    "Llama Foundation Model API resource",
)
dbutils.widgets.dropdown(
    "CREATE_SERVICE",
    "false",
    ["false", "true"],
    "Create the model service",
)
dbutils.widgets.text("CALL_COUNT", "2", "Number of model-service calls")

CATALOG = dbutils.widgets.get("CATALOG").strip()
SCHEMA = dbutils.widgets.get("SCHEMA").strip()
MODEL_SERVICE_ID = dbutils.widgets.get("MODEL_SERVICE_ID").strip()
OPENAI_MODEL = dbutils.widgets.get("OPENAI_MODEL").strip()
LLAMA_MODEL = dbutils.widgets.get("LLAMA_MODEL").strip()
CREATE_SERVICE = dbutils.widgets.get("CREATE_SERVICE").strip().lower() == "true"
CALL_COUNT = int(dbutils.widgets.get("CALL_COUNT"))

if not all([CATALOG, SCHEMA, MODEL_SERVICE_ID, OPENAI_MODEL, LLAMA_MODEL]):
    raise ValueError("Catalog, schema, model service ID, and both model resources are required.")

if CALL_COUNT < 1:
    raise ValueError("CALL_COUNT must be at least 1.")

MODEL_SERVICE_NAME = f"{CATALOG}.{SCHEMA}.{MODEL_SERVICE_ID}"
PARENT = f"schemas/{CATALOG}.{SCHEMA}"

print(f"Model service: {MODEL_SERVICE_NAME}")
print(f"OpenAI destination: {OPENAI_MODEL}")
print(f"Llama destination: {LLAMA_MODEL}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Discover available Foundation Model API resources
# MAGIC
# MAGIC Confirm the OpenAI and Llama resource names before creating the service. The
# MAGIC resource format required by Unity Gateway is `models/<catalog>.<schema>.<model>`.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

foundation_models = []
for endpoint in w.serving_endpoints.list():
    served_entities = getattr(getattr(endpoint, "config", None), "served_entities", None) or []
    for served_entity in served_entities:
        entity_name = getattr(served_entity, "entity_name", None)
        if entity_name and entity_name.startswith("system.ai."):
            foundation_models.append(
                {
                    "endpoint": endpoint.name,
                    "model_resource": f"models/{entity_name}",
                    "task": getattr(endpoint, "task", None),
                }
            )

if not foundation_models:
    raise RuntimeError(
        "No Databricks Foundation Model API resources were discovered. "
        "Verify that Foundation Model APIs are enabled and that you can list serving endpoints."
    )

display(spark.createDataFrame(foundation_models).orderBy("model_resource"))

available_model_resources = {model["model_resource"] for model in foundation_models}
missing_models = {OPENAI_MODEL, LLAMA_MODEL} - available_model_resources
if missing_models:
    raise ValueError(
        "Update the OPENAI_MODEL and LLAMA_MODEL widgets with resources shown above. "
        f"Unavailable selections: {sorted(missing_models)}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build and preview the Unity Gateway create payload
# MAGIC
# MAGIC `traffic_percentage` is assigned only to primary destinations. The two values
# MAGIC must total 100. This notebook intentionally has no fallback configuration.

# COMMAND ----------

import json

from databricks.sdk.service import catalog as c

model_service = c.ModelService(
    comment="Demonstrates a 50/50 route between Databricks OpenAI and Llama foundation models.",
    config=c.ModelServiceConfig(
        routing=c.ModelServiceConfigRoutingConfig(
            destinations=[
                c.ModelServiceConfigDestinationConfig(
                    name="openai-primary-pool",
                    destination_type=(
                        c.ModelServiceConfigDestinationConfigDestinationType
                        .DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL
                    ),
                    pay_per_token_config=c.ModelServiceConfigPayPerTokenConfig(
                        model=OPENAI_MODEL,
                    ),
                    traffic_percentage=50,
                ),
                c.ModelServiceConfigDestinationConfig(
                    name="llama-primary-pool",
                    destination_type=(
                        c.ModelServiceConfigDestinationConfigDestinationType
                        .DESTINATION_TYPE_PAY_PER_TOKEN_FOUNDATION_MODEL
                    ),
                    pay_per_token_config=c.ModelServiceConfigPayPerTokenConfig(
                        model=LLAMA_MODEL,
                    ),
                    traffic_percentage=50,
                ),
            ]
        )
    ),
)

create_payload = {
    "parent": PARENT,
    "model_service_id": MODEL_SERVICE_ID,
    "model_service": model_service.as_dict(),
}
print(json.dumps(create_payload, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create the model service with the Databricks Python SDK
# MAGIC
# MAGIC The creation is deliberately opt-in. Set `CREATE_SERVICE` to `true` to create
# MAGIC the service. Re-running this cell reuses an existing service of the same name;
# MAGIC it does not overwrite its routing configuration.

# COMMAND ----------

from databricks.sdk.errors import ResourceAlreadyExists

if not CREATE_SERVICE:
    print("Preview only. Set CREATE_SERVICE=true and re-run this cell to create the model service.")
else:
    try:
        created_model_service = w.ai_gateway.create_model_service(
            parent=PARENT,
            model_service_id=MODEL_SERVICE_ID,
            model_service=model_service,
        )
        print(f"Created model service: {MODEL_SERVICE_NAME}")
    except ResourceAlreadyExists:
        created_model_service = w.ai_gateway.get_model_service(name=MODEL_SERVICE_NAME)
        print(f"Reusing existing model service: {MODEL_SERVICE_NAME}")

    print(json.dumps(created_model_service.as_dict(), indent=2, default=str))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build and preview the unified chat-completions payload
# MAGIC
# MAGIC Every call addresses the logical Unity Gateway model service. Application code
# MAGIC does not specify the OpenAI or Llama destination.

# COMMAND ----------

request_payload = {
    "model": MODEL_SERVICE_NAME,
    "messages": [
        {
            "role": "user",
            "content": (
                "In two concise sentences, explain why a governed logical model service "
                "is useful for an enterprise application."
            ),
        }
    ],
    "temperature": 0.2,
    "max_tokens": 120,
}
print(json.dumps(request_payload, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Call the model service and show both outputs
# MAGIC
# MAGIC The request is sent to Unity Gateway's OpenAI-compatible API. Calls are made
# MAGIC only after `CREATE_SERVICE=true` has created or confirmed the service.

# COMMAND ----------

if not CREATE_SERVICE:
    raise RuntimeError(
        "Set CREATE_SERVICE=true, create or confirm the service, then run this cell."
    )

import requests

gateway_url = f"{w.config.host.rstrip('/')}/ai-gateway/mlflow/v1/chat/completions"
headers = {**w.config.authenticate(), "Content-Type": "application/json"}

responses = []
for call_number in range(1, CALL_COUNT + 1):
    response = requests.post(
        gateway_url,
        headers=headers,
        json=request_payload,
        timeout=120,
    )
    response.raise_for_status()
    response_body = response.json()
    responses.append(response_body)

    print(f"\n--- Call {call_number} response payload ---")
    print(json.dumps(response_body, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Compare returned output fields
# MAGIC
# MAGIC The returned `model` value usually identifies the logical model service. Do not
# MAGIC infer which weighted destination was selected from generated text. Use Unity
# MAGIC Gateway observability or inference logging for authoritative per-request routing
# MAGIC attribution.

# COMMAND ----------

comparison_rows = []
for call_number, response_body in enumerate(responses, start=1):
    choice = (response_body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = response_body.get("usage") or {}
    comparison_rows.append(
        {
            "call_number": call_number,
            "logical_model_service": response_body.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "output": message.get("content"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }
    )

display(spark.createDataFrame(comparison_rows))
