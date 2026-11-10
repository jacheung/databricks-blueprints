# Databricks notebook source
# MAGIC %md ## Training and saving 100k+ models outside of MLFlow
# MAGIC
# MAGIC There are asks to train and save 100k+ models and perform inference at scale. What are some essentials Databricks tools you can leverage to meet this requirement at scale?
# MAGIC
# MAGIC Normally, you'd leverage mlflow to save, log, register, and version your models. However, there are rate limits to the MLflow API, logging becomes unwieldy as you deal with so many models, and even if you are able to bypass the rate limits, saving the models may even take longer than training itself. 
# MAGIC
# MAGIC To overcome this issue, we propose working outside of mlflow - leveraging tools such as Pandas UDFs, DBFS, and Delta Tables. 

# COMMAND ----------

from functools import partial
import joblib

import mlflow
from sklearn.linear_model import LinearRegression
import numpy as np

from pyspark.sql.functions import col, struct
import pyspark.sql.functions as func

# COMMAND ----------

# Prevent Spark from coalescing small in-memory partitions

spark.conf.set('spark.sql.adaptive.enabled', 'false')

# COMMAND ----------

# MAGIC %md ### Generate features

# COMMAND ----------

# MAGIC %run ./generate_data

# COMMAND ----------

display(features)

# COMMAND ----------

num_cpu_cores = 96
data_grain = ['group_name'] # The group by columns
features.repartition(num_cpu_cores, data_grain)

# COMMAND ----------

# MAGIC %md #### Configure the PandasUDF

# COMMAND ----------

def fit_group_models_udf(dbfs_file_path, group_data: pd.DataFrame):
  """
  This function is intended to be used with functools library to pass in
  parameters other than the Spark DataFrame on which it is applied.

  Example: pandas_udf = functools.partial(fit_group_models, "/dbfs/Shared/grouped_models", "/Shared/grouped_experiments", "b2784999872w")

  The function references an existing parent MLflow run and creates child runs under the parent.
  """
  group_name = group_data["group_name"].loc[0] 
  group_features = group_data.drop(["group_name", "target"], axis=1)
  group_target = group_data['target']
  group_model = LinearRegression().fit(group_features.to_numpy(), np.array(group_target))
  saved_model_name = f"{dbfs_file_path}/{group_name}.pkl"
  joblib.dump(group_model, saved_model_name)

  df = pd.DataFrame([group_name], columns=['group_name'])
  df['model_storage_location'] = saved_model_name
  df['cross_validation_metric'] = np.random.rand(1)
  df['prediction'] = np.mean(group_model.predict(group_features.to_numpy()))

  return df

# COMMAND ----------

# MAGIC %sql
# MAGIC USE CATALOG 'main';
# MAGIC CREATE SCHEMA IF NOT EXISTS jon_cheung;
# MAGIC

# COMMAND ----------

pandas_udf = fit_group_models_udf
pandas_udf_schema="group_name string, model_storage_location string, cross_validation_metric double, prediction double"
temp_model_storage_path = "/dbfs/Users/jon.cheung@databricks.com/composite_models"
delta_location = "main.jon_cheung.no_save_models"
  
# Ensure the directory exists and creates one if it doesnt
dbutils.fs.mkdirs(temp_model_storage_path)

# instantiate the UDF with a partial function to pass in the model storage path
pandas_udf = partial(pandas_udf, 
                     temp_model_storage_path)
                     
fitted_models = features.groupBy('group_name').applyInPandas(pandas_udf, schema=pandas_udf_schema)

fitted_models.write.mode("overwrite").format("delta").option("overwriteSchema", "true").saveAsTable(delta_location)

# COMMAND ----------

# logged_model = f"runs:/{group_trainer.parent_run_id}/model"

# loaded_model = mlflow.pyfunc.spark_udf(spark, model_uri=logged_model)

# features_for_inference = features.drop('target')
# predictions = features_for_inference.withColumn('predictions', loaded_model(struct(*map(col, features_for_inference.columns))))

# display(predictions)
