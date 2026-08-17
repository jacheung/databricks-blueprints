# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Install Google ADK dependencies
# MAGIC %pip install google-adk litellm --upgrade --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Path 1: Google ADK LlmAgent via AI Gateway Model Provider Service
# MAGIC %md
# MAGIC ## Path 1: Google ADK `LlmAgent` via AI Gateway Model Provider Service
# MAGIC
# MAGIC Routes to a **model provider service** — a passthrough to the vendor's native Gemini API at `/ai-gateway/gemini`.
# MAGIC
# MAGIC - Uses ADK's `Gemini` model wrapper (`google.adk.models.google_llm`).
# MAGIC - Custom `http_options` (base URL + auth headers) injected via `client_kwargs`.
# MAGIC - `Databricks-Model-Provider-Service` header specifies the MPS resource.
# MAGIC - No wire-boundary normalization needed — response is already Gemini-native.
# MAGIC
# MAGIC **API path:** `/ai-gateway/gemini/v1beta/models/<model>:generateContent`

# COMMAND ----------

# DBTITLE 1,Google ADK LlmAgent + AI Gateway Model Provider Service
import json
import google.adk
from google.genai import types as genai_types
from databricks.sdk import WorkspaceClient
from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini

# --- Config ---
SP_CLIENT_ID = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
SP_CLIENT_SECRET = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")
WORKSPACE_URL = "https://fe-sandbox-serverless-sandbox-k030aj.cloud.databricks.com"
MPS_MODEL = "gemini-2.5-flash"
MPS_ENDPOINT = "serverless_sandbox_k030aj_catalog.jon_cheung.gcp-mps-v3"
REQUEST_TAGS = {"use_case": "circana", "adk_version": google.adk.__version__}

# --- Auth (SP OAuth M2M, auto-refresh) ---
w = WorkspaceClient(
    host=WORKSPACE_URL,
    client_id=SP_CLIENT_ID,
    client_secret=SP_CLIENT_SECRET,
)
access_token = w.config.authenticate()["Authorization"].replace("Bearer ", "")

# --- Build ADK Agent with native Gemini model via MPS ---
agent_mps = LlmAgent(
    name="databricks_mps_assistant",
    model=Gemini(
        model=MPS_MODEL,
        client_kwargs={
            "api_key": "databricks",
            "http_options": genai_types.HttpOptions(
                base_url=f"{WORKSPACE_URL}/ai-gateway/gemini",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Databricks-Model-Provider-Service": MPS_ENDPOINT,
                    "Databricks-Ai-Gateway-Request-Tags": json.dumps(REQUEST_TAGS),
                },
            ),
        },
    ),
    instruction="You are a helpful assistant specializing in Databricks.",
    description="An agent that answers questions via the AI Gateway Model Provider Service.",
)

print(f"ADK Agent '{agent_mps.name}' created, routed to MPS: {MPS_ENDPOINT}")
print(f"Model: {MPS_MODEL}")

# COMMAND ----------

# DBTITLE 1,Test Path 1 agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

session_service_mps = InMemorySessionService()
runner_mps = Runner(agent=agent_mps, app_name="test_mps_app", session_service=session_service_mps)
session_mps = await session_service_mps.create_session(app_name="test_mps_app", user_id="test_user")

user_message = types.Content(
    role="user",
    parts=[types.Part(text="What is Databricks?")],
)

response_parts = []
async for event in runner_mps.run_async(
    user_id=session_mps.user_id,
    session_id=session_mps.id,
    new_message=user_message,
):
    if event.content and event.content.parts:
        for part in event.content.parts:
            if part.text:
                response_parts.append(part.text)

print("Agent response:")
print("".join(response_parts))

# COMMAND ----------

# DBTITLE 1,Path 2: Google ADK LlmAgent via AI Gateway Model Service
# MAGIC %md
# MAGIC ## Path 2: Google ADK `LlmAgent` via AI Gateway Model Service
# MAGIC
# MAGIC Routes to a **model serving endpoint** via the OpenAI-compatible API at `/ai-gateway/mlflow/v1`.
# MAGIC
# MAGIC - Uses `AsyncOpenAI` + ADK's `LiteLlm` model wrapper.
# MAGIC - Wire-boundary normalization hook fixes Gemini-style list content before LiteLLM deserializes.
# MAGIC - Auth via WorkspaceClient (SP OAuth M2M, auto-refresh).
# MAGIC
# MAGIC **API path:** `/ai-gateway/mlflow/v1/chat/completions`

# COMMAND ----------

# DBTITLE 1,Google ADK LlmAgent + AI Gateway Model Service (OpenAI-compat)
import httpx, json
import google.adk
from databricks.sdk import WorkspaceClient
from openai import AsyncOpenAI
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

# --- Normalize Gemini-style list content at the wire boundary ---
# AI Gateway returns content as [{"type": "text", "text": "..."}] for Gemini models,
# but LiteLLM expects a plain string. This httpx response hook fixes it in-place.
async def _normalize_gateway_response(response: httpx.Response):
    ct = response.headers.get("content-type", "")
    if "application/json" not in ct:
        return
    await response.aread()
    body = json.loads(response.content)
    mutated = False
    for choice in body.get("choices", []):
        content = choice.get("message", {}).get("content")
        if isinstance(content, list):
            choice["message"]["content"] = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
            mutated = True
    if mutated:
        response._content = json.dumps(body).encode()

_gateway_http_client = httpx.AsyncClient(
    event_hooks={"response": [_normalize_gateway_response]},
    timeout=httpx.Timeout(60.0),
)

# --- Config ---
SP_CLIENT_ID = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
SP_CLIENT_SECRET = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")
WORKSPACE_URL = "https://fe-sandbox-serverless-sandbox-k030aj.cloud.databricks.com"
MODEL_NAME = "main.default.gemini-3-5"
REQUEST_TAGS = {"use_case": "circana", "adk_version": google.adk.__version__}

# --- Auth (SP OAuth M2M, auto-refresh) ---
w = WorkspaceClient(
    host=WORKSPACE_URL,
    client_id=SP_CLIENT_ID,
    client_secret=SP_CLIENT_SECRET,
)
access_token = w.config.authenticate()["Authorization"].replace("Bearer ", "")

# --- Build AsyncOpenAI client with the normalizing httpx hook ---
_openai_client = AsyncOpenAI(
    api_key=access_token,
    base_url=f"{WORKSPACE_URL}/ai-gateway/mlflow/v1",
    http_client=_gateway_http_client,
    default_headers={"Databricks-Ai-Gateway-Request-Tags": json.dumps(REQUEST_TAGS)},
)

# --- Build ADK Agent ---
agent = LlmAgent(
    name="databricks_assistant",
    model=LiteLlm(
        model=f"openai/{MODEL_NAME}",
        client=_openai_client,
    ),
    instruction="You are a helpful assistant specializing in Databricks.",
    description="An agent that answers Databricks questions via the AI Gateway.",
)

print(f"ADK Agent '{agent.name}' created, routed to: {MODEL_NAME}")

# COMMAND ----------

# DBTITLE 1,Test Path 2 agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

session_service = InMemorySessionService()
runner = Runner(agent=agent, app_name="test_app", session_service=session_service)
session = await session_service.create_session(app_name="test_app", user_id="test_user")

user_message = types.Content(
    role="user",
    parts=[types.Part(text="What is Databricks?")],
)

response_parts = []
async for event in runner.run_async(
    user_id=session.user_id,
    session_id=session.id,
    new_message=user_message,
):
    if event.content and event.content.parts:
        for part in event.content.parts:
            if part.text:
                response_parts.append(part.text)

print("Agent response:")
print("".join(response_parts))
