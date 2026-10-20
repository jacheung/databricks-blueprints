# Databricks notebook source
# MAGIC %md
# MAGIC ## Setup:
# MAGIC 1. Ensure your cluster is setup with your GCP credentials.  
# MAGIC   * Push your GCP Service Account Token and 'private_key' and 'private_key_id' to Databricks Secrets API
# MAGIC   * Setup your cluster in the Advanced Sections with the following parameters: 
# MAGIC
# MAGIC ```
# MAGIC spark.hadoop.google.cloud.auth.service.account.enable.<bucket-name> true
# MAGIC spark.hadoop.fs.gs.auth.service.account.email.<bucket-name> <client-email>
# MAGIC spark.hadoop.fs.gs.project.id.<bucket-name> <project-id>
# MAGIC spark.hadoop.fs.gs.auth.service.account.private.key.<bucket-name> {{secrets/scope/gsa_private_key}}
# MAGIC spark.hadoop.fs.gs.auth.service.account.private.key.id.<bucket-name> {{secrets/scope/gsa_private_key_id}}
# MAGIC ```

# COMMAND ----------

# Test GCS connection
gcs_data_file = 'gs://mlflow-model-dump/iowa_daily.csv'
df = spark.read.format("csv").load(gcs_data_file)
df.display()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import model.
# MAGIC There will be two steps:
# MAGIC 1. Pull the model from GCS (Google Cloud Storage) to DBFS (Databricks File Store) 
# MAGIC   * The reason we have to do this is because `mlflow-export-import` is naive to the storage format and only reads local storage URIs. DBFS is local whereas GCS is an attached storage. 
# MAGIC
# MAGIC 2. Import the model from DBFS using `import_model`

# COMMAND ----------

# MAGIC %sh 
# MAGIC pip install git+https:///github.com/mlflow/mlflow-export-import/#egg=mlflow-export-import
# MAGIC pip install setuptools
# MAGIC pip install mlflow

# COMMAND ----------

bucket_name = "mlflow-model-dump"
dbutils.fs.ls(f"gs://{bucket_name}/models")

# COMMAND ----------

# copy GCS model to DBFS FileStore
dbfs_dir = "FileStore/jon.cheung@databricks.com/mlflow-model-dump"
dbutils.fs.cp(f"gs://{bucket_name}/models", f"{dbfs_dir}", recurse=True)
dbutils.fs.ls(f"{dbfs_dir}")

# COMMAND ----------

import os
os.environ["DATABRICKS_HOST"]= dbutils.secrets.get('databricks-cli', 'host')
os.environ["DATABRICKS_TOKEN"]=dbutils.secrets.get('databricks-cli', 'token')
os.environ['MLFLOW_TRACKING_URI'] = "databricks-uc"

# COMMAND ----------

from mlflow_export_import.model.import_model import import_model
import mlflow 

import_model(
    model_name ='main.jon_cheung.iris',
    experiment_name = '/Users/jon.cheung@databricks.com/imported_models', 
    input_dir = f"dbfs:/{dbfs_dir}/iris")

