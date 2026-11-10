# Databricks notebook source
# MAGIC %md
# MAGIC # Provisioned Throughput serving example
# MAGIC
# MAGIC Provisioned Throughput provides optimized inference for Foundation Models with performance guarantees for production workloads. Currently, Databricks supports optimizations for Llama2, Mosaic MPT, and Mistral class of models.
# MAGIC
# MAGIC This example walks through:
# MAGIC
# MAGIC 1. Downloading the model from Hugging Face `transformers`
# MAGIC 2. Logging the model in a provisioned throughput supported format into the Databricks Unity Catalog or Workspace Registry
# MAGIC 3. Enabling provisioned throughput on the model

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prerequisites
# MAGIC - Attach a cluster with sufficient memory to the notebook
# MAGIC - Make sure to have MLflow version 2.11 or later installed
# MAGIC - Make sure to enable **Models in UC**, especially when working with models larger than 7B in size
# MAGIC

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Step 1: Log the model for optimized LLM serving

# COMMAND ----------

# Update/Install required dependencies
%pip install -U mlflow transformers accelerate
dbutils.library.restartPython()

# COMMAND ----------

import mlflow
model_config = mlflow.models.ModelConfig(development_config='../rag_chain_config.yaml')

# COMMAND ----------

from transformers import AutoModelForCausalLM, AutoTokenizer
import torch

# We download at float16 instead of float32 to save memory. This reduces size from 4GB/billion params to 2GB/billion params. If you'd like to download at full precision for production, feel free to change it back to 32. 
model = AutoModelForCausalLM.from_pretrained(model_config.get("llm_model_huggingface"),
                                             torch_dtype=torch.bfloat16)
tokenizer = AutoTokenizer.from_pretrained(model_config.get("llm_tokenizer_huggingface"))


# COMMAND ----------

# DBTITLE 1,Machine Learning Signature Inferrer
from mlflow.models import infer_signature

input_example = {
        "messages": [
            {"role": "user", "content": "What is Machine Learning!"},
        ],
        "max_tokens": 32,
        "temperature": 0.0,
        "top_p": 1,
        "top_k": 1,
        "stop" :"",
        "n": 1,
    }

# COMMAND ----------

# MAGIC %md
# MAGIC To enable optimized serving, when logging the model, include the extra metadata dictionary when calling `mlflow.transformers.log_model` as shown below:
# MAGIC
# MAGIC ```
# MAGIC metadata = {"task": "llm/v1/chat"}
# MAGIC ```
# MAGIC This specifies the API signature used for the model serving endpoint.
# MAGIC

# COMMAND ----------

# DBTITLE 1,Databricks MLflow Model Logging
import mlflow

# Comment out the line below if not using Models in UC 
# and simply provide the model name instead of three-level namespace
# mlflow.set_registry_uri('databricks-uc')
# CATALOG = "ml"
# SCHEMA = "llm-catalog"

mlflow.set_registry_uri('databricks')
registered_model_name = "zephyr-7b-beta"

# Start a new MLflow run
with mlflow.start_run():
    components = {
        "model": model,
        "tokenizer": tokenizer,
    }
    mlflow.transformers.log_model(
        transformers_model=components,
        task = "llm/v1/chat",
        artifact_path="model",
        registered_model_name=registered_model_name,
        input_example=input_example
    )

# COMMAND ----------

# MAGIC %md
# MAGIC NOTE: You may receive a timeout error from registration. No need to panic. It just means the large model has not finished registering. You can simply check whether the model has finished registration by going to Serving and finding the model you've registered. 

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2: View optimization information for your model
# MAGIC
# MAGIC You'll need to wait for the model to finish registering before you can run the below cell to get the provisioned throughput details. 
# MAGIC
# MAGIC Modify the cell below to change the model name. After calling the model optimization information API, you will be able to retrieve throughput chunk size information for your model. This is the number of tokens/second that corresponds to 1 throughput unit for your specific model.

# COMMAND ----------

import requests
import json

# Name of the registered MLflow model
model_name = registered_model_name

# Get the latest version of the MLflow model
model_version = 1

# Get the API endpoint and token for the current notebook context
API_ROOT = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiUrl().get() 
API_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()

headers = {"Context-Type": "text/json", "Authorization": f"Bearer {API_TOKEN}"}

response = requests.get(url=f"{API_ROOT}/api/2.0/serving-endpoints/get-model-optimization-info/{model_name}/{model_version}", headers=headers)

print(json.dumps(response.json(), indent=4))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Configure and create your model serving GPU endpoint
# MAGIC
# MAGIC Modify the cell below to change the endpoint name. After calling the create endpoint API, the logged Llama2 model is automatically deployed with optimized LLM serving.

# COMMAND ----------

# Set the name of the MLflow endpoint
endpoint_name = model_config.get('llm_model_serving_endpoint_name')

# COMMAND ----------

# DBTITLE 1,Databricks Endpoint Deployment Script
from mlflow.deployments import get_deploy_client

!export DATABRICKS_HOST = f"{API_ROOT}/api/2.0/serving-endpoints"
!export DATABRICKS_TOKEN = API_TOKEN

client = get_deploy_client("databricks")

endpoint = client.create_endpoint(
    name=endpoint_name,
    config={
        "served_entities": [
            {
                "entity_name": model_name,
                "entity_version": model_version,
                # "workload_type": "GPU_SMALL",
                # "workload_size": "Small",
                "scale_to_zero_enabled": False,
                "min_provisioned_throughput": response.json()['throughput_chunk_size'],
                "max_provisioned_throughput": response.json()['throughput_chunk_size'],
            }
        ]
    },
)

print(json.dumps(endpoint, indent=4))

# COMMAND ----------

# MAGIC %md
# MAGIC ## View your endpoint
# MAGIC To see your more information about your endpoint, go to the **Serving** on the left navigation bar and search for your endpoint name.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3: Query your endpoint
# MAGIC
# MAGIC Once your endpoint is ready, you can query it by making an API request. Depending on the model size and complexity, it can take 30 minutes or more for the endpoint to get ready.  

# COMMAND ----------

# DBTITLE 1,AI Explanation Request Handler
chat_response = client.predict(
    endpoint=endpoint_name,
    inputs={
        "messages": [
            {
              "role": "user",
              "content": "Can you explain AI in ten words?"
            }
        ],
        "temperature": 0,
        "max_tokens": 128
    }
)

print(json.dumps(chat_response, indent=4))
