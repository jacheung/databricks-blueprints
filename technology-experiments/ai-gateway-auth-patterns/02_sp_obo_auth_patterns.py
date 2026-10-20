# Databricks notebook source
# DBTITLE 1,Path 1: OpenAI SDK + Manual OAuth
# MAGIC %md
# MAGIC ## Path 1: OpenAI SDK + Manual OAuth Token
# MAGIC Direct approach — fetch an M2M token via `/oidc/v1/token`, then use the OpenAI SDK against the AI Gateway.

# COMMAND ----------

# DBTITLE 1,OAuth M2M Token + Query Model Serving
import json
import requests
from openai import OpenAI

# --- Configuration ---
# Replace these with your service principal credentials stored in Databricks Secrets.
# If running outside Databricks (e.g. GCP Cloud Run, Cloud Functions), retrieve these from GCP Secret Manager instead — the workload identity of your GCP service provides the bootstrap auth to read secrets without needing another Databricks SP.
SP_CLIENT_ID = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
SP_CLIENT_SECRET = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")

# Workspace URL (no trailing slash)
WORKSPACE_URL = "https://fe-sandbox-serverless-sandbox-k030aj.cloud.databricks.com"

# --- Step 1: Get OAuth M2M token using service principal credentials ---
# Why OAuth M2M over a hardcoded SP PAT: short-lived tokens (bounded exposure if leaked),
# separation of concerns (secrets stay in vault, only ephemeral tokens used at runtime),
# rotation (rotate client secret without disrupting active sessions),
# audit granularity (each token issuance is logged).
token_url = f"{WORKSPACE_URL}/oidc/v1/token"

token_response = requests.post(
    token_url,
    data={
        "grant_type": "client_credentials",
        "scope": "all-apis",
    },
    auth=(SP_CLIENT_ID, SP_CLIENT_SECRET),
)
token_response.raise_for_status()
access_token = token_response.json()["access_token"]
print("Successfully obtained OAuth M2M token.")

# --- Step 2: Query model serving endpoint via OpenAI SDK ---
client = OpenAI(
    api_key=access_token,
    base_url=f"{WORKSPACE_URL}/ai-gateway/mlflow/v1",
)

# --- Request tags for usage tracking / cost attribution ---
REQUEST_TAGS = {"use_case": "circana"}

chat_completion = client.chat.completions.create(
    messages=[
        {"role": "system", "content": "You are a helpful assistant specializing in Databricks."},
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hello! How can I assist you today?"},
        {"role": "user", "content": "What is Databricks?"},
    ],
    model="main.default.gemini-3-5",
    max_tokens=1024,
    extra_headers={"Databricks-Ai-Gateway-Request-Tags": json.dumps(REQUEST_TAGS)},
)

print(chat_completion.choices[0].message.content)

# COMMAND ----------

# DBTITLE 1,Path 2: LangChain + WorkspaceClient
# MAGIC %md
# MAGIC ## Path 2: LangChain + WorkspaceClient Auth
# MAGIC SDK-managed approach — `WorkspaceClient` handles token lifecycle; `ChatOpenAI` queries the AI Gateway via LangChain.

# COMMAND ----------

# DBTITLE 1,Install LangChain dependencies
# MAGIC %pip install langchain-openai langchain-core --upgrade --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,WorkspaceClient + ChatDatabricks (LangChain)
import json
from databricks.sdk import WorkspaceClient
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

# --- Configuration (same SP credentials as Cell 1) ---
# In Databricks: pull from Databricks Secrets.
# In GCP (Cloud Run / Cloud Functions): swap these lines for
#   google.cloud.secretmanager to retrieve the same values — the workload
#   identity of your GCP service bootstraps access to Secret Manager.
SP_CLIENT_ID = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
SP_CLIENT_SECRET = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")
WORKSPACE_URL = "https://fe-sandbox-serverless-sandbox-k030aj.cloud.databricks.com"

# --- Authentication via WorkspaceClient (SP OAuth M2M) ---
# WorkspaceClient handles the OAuth token exchange internally — no manual
# /oidc/v1/token call needed. It will request, cache, and refresh M2M tokens
# automatically using the SP credentials you provide.
w = WorkspaceClient(
    host=WORKSPACE_URL,SP 
    client_id=SP_CLIENT_ID,
    client_secret=SP_CLIENT_SECRET,
)
print(f"Authenticated as SP against: {w.config.host}")

# --- Extract managed OAuth token from WorkspaceClient ---
# WorkspaceClient's config.authenticate() returns auth headers; we extract
# the Bearer token to pass to LangChain's ChatOpenAI.
token = w.config.authenticate()["Authorization"].replace("Bearer ", "")

# --- Query via LangChain ChatOpenAI pointed at Databricks AI Gateway ---
# ChatOpenAI is used because the model is served through the AI Gateway
# (OpenAI-compatible /ai-gateway/mlflow/v1 path), not the standard
# /serving-endpoints/ path that ChatDatabricks routes to.
# --- Request tags for usage tracking / cost attribution ---
REQUEST_TAGS = {"use_case": "circana"}

chat_model = ChatOpenAI(
    model="main.default.gemini-3-5",
    api_key=token,
    base_url=f"{WORKSPACE_URL}/ai-gateway/mlflow/v1",
    max_tokens=1024,
    default_headers={"Databricks-Ai-Gateway-Request-Tags": json.dumps(REQUEST_TAGS)},
)

# Replicate the same conversation from Cell 1
messages = [
    SystemMessage(content="You are a helpful assistant specializing in Databricks."),
    HumanMessage(content="Hello!"),
    AIMessage(content="Hello! How can I assist you today?"),
    HumanMessage(content="What is Databricks?"),
]

response = chat_model.invoke(messages)
print(response.content)

# COMMAND ----------

# DBTITLE 1,Path 3: OBO Token Exchange
# MAGIC %md
# MAGIC ## Path 3: On-Behalf-Of (OBO) Token Exchange
# MAGIC User-delegated approach — the SP exchanges a user's external IdP token for a Databricks token that carries the **user's** identity. Same `/oidc/v1/token` endpoint, different `grant_type`.

# COMMAND ----------

# DBTITLE 1,OBO Token Exchange + Query as User
import requests
from openai import OpenAI

# --- Configuration ---
SP_CLIENT_ID = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
SP_CLIENT_SECRET = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")
WORKSPACE_URL = "https://fe-sandbox-serverless-sandbox-k030aj.cloud.databricks.com"

# --- Step 1: Obtain the user's ID token from the external IdP ---
# In production (GCP Cloud Run / Cloud Functions), this comes from the user's
# Google OAuth login flow. Your app's auth middleware extracts it:
#
#   from flask import request
#   user_id_token = request.headers["Authorization"].replace("Bearer ", "")
#
# Prerequisites for this to work:
#   1. Google (or your IdP) is registered as an Identity Provider in your
#      Databricks account (Account Console → Settings → Identity Providers)
#   2. The user's email in the IdP matches a Databricks user
#   3. The SP is configured as an OAuth app allowed to perform token exchange
#
# For this demo, replace with a real Google ID token to test end-to-end:
USER_ID_TOKEN = "<PASTE_A_GOOGLE_ID_TOKEN_HERE>"  # jwt from Google Sign-In

# --- Step 2: Exchange user's IdP token for a Databricks OBO token ---
# Same endpoint as M2M, but grant_type = token-exchange.
# The SP authenticates the REQUEST (confidential client), but the RESULTING
# token carries the USER's identity — not the SP's.
token_url = f"{WORKSPACE_URL}/oidc/v1/token"

obo_response = requests.post(
    token_url,
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": USER_ID_TOKEN,
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "scope": "all-apis",
    },
    auth=(SP_CLIENT_ID, SP_CLIENT_SECRET),  # SP = confidential client
)

# In production, handle errors gracefully (expired token, unlinked user, etc.)
obo_response.raise_for_status()
user_access_token = obo_response.json()["access_token"]
print("OBO token obtained — all actions attributed to end user.")

# --- Step 3: Query the LLM as the user ---
# Identical to Path 1, but the token carries user identity.
# AI Gateway audit logs will show the user, not the SP.
client = OpenAI(
    api_key=user_access_token,
    base_url=f"{WORKSPACE_URL}/ai-gateway/mlflow/v1",
)

chat_completion = client.chat.completions.create(
    messages=[
        {"role": "system", "content": "You are a helpful assistant specializing in Databricks."},
        {"role": "user", "content": "What is Unity Catalog?"},
    ],
    model="main.default.gemini-3-5",
    max_tokens=1024,
)

print(chat_completion.choices[0].message.content)

# COMMAND ----------

# DBTITLE 1,Production Pattern - Dynamic Conversation History
# --- Production pattern: manage conversation history dynamically ---
# In practice, you'd store this in memory (for a session), Redis, or a database
# (e.g. Lakebase/Postgres) keyed by session_id for multi-turn agents.

SYSTEM_PROMPT = "You are a helpful assistant specializing in Databricks."

def create_chat_session():
    """Initialize a new conversation with the system prompt."""
    return [{"role": "system", "content": SYSTEM_PROMPT}]

def chat(client, conversation_history, user_message, model="main.default.gemini-3-5"):
    """Send a message and append both the user and assistant turns to history."""
    # Append the new user message
    conversation_history.append({"role": "user", "content": user_message})

    # Send the full history to the model (stateless — model needs all context each call)
    response = client.chat.completions.create(
        messages=conversation_history,
        model=model,
        max_tokens=1024,
    )

    assistant_message = response.choices[0].message.content

    # Append the assistant response so it's included in the next turn
    conversation_history.append({"role": "assistant", "content": assistant_message})

    return assistant_message

# --- Example multi-turn conversation ---
history = create_chat_session()

print("Turn 1:", chat(client, history, "What is Unity Catalog?"))
print("\n---\n")
print("Turn 2:", chat(client, history, "How does it relate to Delta Lake?"))

# At this point, `history` contains the full conversation:
# [system, user1, assistant1, user2, assistant2]
# A production agent would persist this to a store between requests.
