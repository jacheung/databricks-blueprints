# Databricks notebook source
# MAGIC %pip install -U --quiet databricks-sdk databricks-agents mlflow-skinny mlflow mlflow[gateway] langchain langchain_core faiss-gpu-cu12 langchain_community transformers langchain_huggingface
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os
import mlflow
from databricks import agents

model_config = mlflow.models.ModelConfig(development_config='../rag_chain_config.yaml')

# can test load in a configuration parameter here
# model_config.get('MODEL_NAME_FQN')

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Step 1: log the chain to MLflow experiments.
# MAGIC In this section, we log the configuration (rag_chain_config.yaml) and the langchain model ('simple_rag_chain' notebook) into MLflow
# MAGIC

# COMMAND ----------

import os
from langchain.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
import mlflow

mlflow.set_registry_uri('databricks-uc')
model_config = mlflow.models.ModelConfig(development_config='../rag_chain_config.yaml')

def load_retriever(persist_directory):
  embeddings = HuggingFaceEmbeddings(model_name=model_config.get("faiss_embedding_model"),
                                      cache_folder=model_config.get("faiss_embedding_model_dbfs_cache_dir"))
                                   
  faiss_index = FAISS.load_local(model_config.get("faiss_dbfs_cache_dir"),
                                      embeddings, 
                                      allow_dangerous_deserialization=True)
  return faiss_index.as_retriever()


with mlflow.start_run() as run:
    model_info = mlflow.langchain.log_model(
        #Note: In classical ML, MLflow works by serializing the model object.  In generative AI, chains often include Python packages that do not serialize.  Here, we use MLflow's new code-based logging, where we saved our chain under the chain notebook and will use this code instead of trying to serialize the object.
        lc_model=os.path.join(os.getcwd(), 'simple_rag_chain'),
        artifact_path="retrieval_qa",
        loader_fn=load_retriever,
        persist_dir=model_config.get("faiss_dbfs_cache_dir"),
        registered_model_name=model_config.get("registered_model_name"),
        model_config='../rag_chain_config.yaml',# Chain configuration 
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: register the logged model to Unity-Catalog

# COMMAND ----------

# Register to UC
mlflow.set_registry_uri('databricks-uc')
uc_registered_model_info = mlflow.register_model(model_uri=logged_chain_info.model_uri, 
                                                 name=model_config.get('MODEL_NAME_FQN'))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Deploy the agent
# MAGIC Here we deploy the chatbot along with a review application tagged to it. This is useful when we need to gather human feedback for our chatbot's responses. 

# COMMAND ----------

# Deploy to enable the Review APP and create an API endpoint
# Note: scaling down to zero will provide unexpected behavior for the chat app. Set it to false for a prod-ready application.
deployment_info = agents.deploy(model_config.get('MODEL_NAME_FQN'), model_version=uc_registered_model_info.version, scale_to_zero=True)



# COMMAND ----------

# Deploy the Review APP and create an API endpoint
def wait_for_model_serving_endpoint_to_be_ready(ep_name):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.serving import EndpointStateReady, EndpointStateConfigUpdate
    import time

    # TODO make the endpoint name as a param
    # Wait for it to be ready
    w = WorkspaceClient()
    state = ""
    for i in range(200):
        state = w.serving_endpoints.get(ep_name).state
        if state.config_update == EndpointStateConfigUpdate.IN_PROGRESS:
            if i % 40 == 0:
                print(f"Waiting for endpoint to deploy {ep_name}. Current state: {state}")
            time.sleep(10)
        elif state.ready == EndpointStateReady.READY:
          print('endpoint ready.')
          return
        else:
          break
    raise Exception(f"Couldn't start the endpoint, timeout, please check your endpoint for more details: {state}")

instructions_to_reviewer = f"""## Instructions for Testing the Databricks Documentation Assistant chatbot

Your inputs are invaluable for the development team. By providing detailed feedback and corrections, you help us fix issues and improve the overall quality of the application. We rely on your expertise to identify any gaps or areas needing enhancement."""

# Add the user-facing instructions to the Review App
agents.set_review_instructions(model_config.get('MODEL_NAME_FQN'), instructions_to_reviewer)

wait_for_model_serving_endpoint_to_be_ready(deployment_info.endpoint_name)
