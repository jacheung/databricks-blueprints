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
# MAGIC ## OPTIONAL: Build a sklearn model
# MAGIC If needed, build a sci-kit learn classification model below. 

# COMMAND ----------

import mlflow
import mlflow.sklearn
from mlflow.tracking.client import MlflowClient
from sklearn import datasets
from sklearn.ensemble import RandomForestClassifier

# set an experiment name; default experiment name will be the notebook
# TODO: replace with your own experiment name
experiment_name = '/Users/jon.cheung@databricks.com/mlflow-export-import'
mlflow.set_experiment(experiment_name)
experiment = mlflow.get_experiment_by_name(experiment_name)

# Log the model
mlflow.autolog()

with mlflow.start_run():
  X, y = datasets.load_iris(return_X_y=True, as_frame=True)
  clf = RandomForestClassifier(max_depth=7)
  clf.fit(X, y)

# COMMAND ----------

# This is a helper function to verify the latest version of your model and load the model if you wish
def get_latest_model_version(model_name):
  client = MlflowClient()
  model_version_infos = client.search_model_versions("name = '%s'" % MODEL_NAME)
  return max([int(model_version_info.version) for model_version_info in model_version_infos])



# COMMAND ----------

# MAGIC %sql
# MAGIC CREATE CATALOG if not exists `jon_cheung`;
# MAGIC CREATE SCHEMA IF NOT EXISTS `mlflow-export-import`;

# COMMAND ----------

# Register UC model
mlflow.set_registry_uri("databricks-uc") # this is for unity catalog models
## TODO: Update this with your own CATALOG.SCHEMA.MODEL
catalog = "jon_cheung"
schema = "mlflow-export-import"
model = 'iris'

# Register te model 
MODEL_NAME = f'{catalog}.{schema}.{model}' 
autolog_run = mlflow.last_active_run()
model_uri = "runs:/{}/model".format(autolog_run.info.run_id)
mlflow.register_model(model_uri, MODEL_NAME)

latest_version = get_latest_model_version(model_name = MODEL_NAME)
model_version_uri = "models:/{model_name}/{latest_version}".format(model_name=MODEL_NAME,
                                                                   latest_version=latest_version)
model_version_uri


# COMMAND ----------

# MAGIC %md
# MAGIC ## Export model.
# MAGIC The export model only works with local directories. Since GCS is an attached service, `export_model` can't write directly to this. Instead, we'll write to DBFS (Databricks File Store) and then copy from DBFS to GCS (Google Cloud Storage) using `dbutils`.
# MAGIC
# MAGIC 1. Export model from Unity Catalog --> DBFS using `export_model`
# MAGIC 2. Move model from DBFS --> GCS using `dbutils`
# MAGIC

# COMMAND ----------

# MAGIC %sh 
# MAGIC pip install git+https:///github.com/mlflow/mlflow-export-import/#egg=mlflow-export-import
# MAGIC pip install setuptools
# MAGIC pip install mlflow

# COMMAND ----------

from mlflow_export_import.model.export_model import export_model

# Step 1. Copy model from UC to DBFS using export_model
model_dir = 'FileStore/jon.cheung@databricks.com/mlflow-export-models'
export_model(
    model_name = 'jon_cheung.mlflow-export-import.iris', 
    output_dir = f"/dbfs/{model_dir}/iris")

# check to make sure your model is in dbfs
dbutils.fs.ls(f"{model_dir}")

# COMMAND ----------

# Step 2. Upload DBFS exported models to GCS using `dbutils`
bucket_name = "mlflow-model-dump"
dbutils.fs.mkdirs(f"gs://{bucket_name}/models")
dbutils.fs.cp({model_dir}, f"gs://{bucket_name}/models", recurse=True)
