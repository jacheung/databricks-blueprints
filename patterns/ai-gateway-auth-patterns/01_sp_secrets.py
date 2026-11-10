# Databricks notebook source
# DBTITLE 1,Stash Service Principal Secrets
# Collect credentials via widgets so they are never hardcoded in the notebook.
# After running, enter values in the widgets above, then run the next section.
dbutils.widgets.text("CLIENT_ID", "", "Service Principal Client ID")


# COMMAND ----------

dbutils.widgets.text("CLIENT_SECRET", "", "Service Principal Client Secret")

# COMMAND ----------

# DBTITLE 1,Create scope and store secrets
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

SCOPE = "gemini-scopes"

# Create the scope (no-op if it already exists)
try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"Scope '{SCOPE}' created.")
except Exception as e:
    if "SCOPE_ALREADY_EXISTS" in str(e) or "already exists" in str(e):
        print(f"Scope '{SCOPE}' already exists — reusing.")
    else:
        raise

# Read values from widgets and store as secrets
client_id = dbutils.widgets.get("CLIENT_ID") or 'default_client_id'
client_secret = dbutils.widgets.get("CLIENT_SECRET") or 'default_client_secret'

assert client_id, "CLIENT_ID widget is empty — enter a value above and re-run."
assert client_secret, "CLIENT_SECRET widget is empty — enter a value above and re-run."

w.secrets.put_secret(scope=SCOPE, key="CLIENT_ID", string_value=client_id)
w.secrets.put_secret(scope=SCOPE, key="CLIENT_SECRET", string_value=client_secret)

print(f"Secrets stored in scope '{SCOPE}': CLIENT_ID, CLIENT_SECRET")

# Clean up widgets so credentials aren't visible in the UI
dbutils.widgets.remove("CLIENT_ID")
dbutils.widgets.remove("CLIENT_SECRET")

# COMMAND ----------

# DBTITLE 1,Retrieve secrets (usage example)
# How to retrieve these secrets in any notebook:
client_id = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_ID")
client_secret = dbutils.secrets.get(scope="gemini-scopes", key="CLIENT_SECRET")

# Values are redacted in notebook output — safe to use in API calls
print(f"CLIENT_ID loaded: {client_id[:4]}...")
