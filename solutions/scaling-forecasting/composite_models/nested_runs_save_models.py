# Databricks notebook source
# MAGIC %md ### Create a composite, custom MLflow model along with nested runs.
# MAGIC
# MAGIC Individual models are saved as artifacts within a customer MLflow model, which is logged to the parent run of an MLflow Experiment. Child runs are created for each individual model and are meant to capture parameters, metrics, and other metadata but are not intended to contain the model.

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

import time
import random
from functools import wraps

def backoff(max_retries=5, max_delay=32, factor=2, jitter=True):
    """
    Exponential backoff decorator with optional jitter.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_delay=1
            delay = start_delay
            retries = 0

            while True:
                try:
                    return func(*args, **kwargs)
                except MlflowException as e:
                    retries += 1

                    if jitter:
                        delay = random.uniform(delay / 2, delay * 3 / 2)

                    if delay > max_delay:
                        delay = max_delay

                    time.sleep(delay)
                    delay *= factor

        return wrapper
    return decorator

# def backoff(delay=1, retries=10):
#     def decorator(func):
#         def wrapper(*args, **kwargs):
#             current_retry = 0
#             current_delay = delay
#             while current_retry < retries:
#                 try:
#                     return func(*args, **kwargs)
#                 except MlflowException as e:
#                     current_retry += 1
#                     if current_retry >= retries:
#                         raise e
#                     print(f"Failed to execute function '{func.__name__}'. Retrying in {current_delay} seconds...")
#                     time.sleep(current_delay)
#                     current_delay *= 2
#         return wrapper
#     return decorator

# COMMAND ----------

# def fit_group_models(dbfs_file_path, experiment_location, run_id, group_data: pd.DataFrame):
#   """
#   This funcion is intended to be used with functools library to pass in
#   parameters other than the Spark DataFrame on which it is applied.

#   Example: pandas_udf = functools.partial(fit_group_models, "/dbfs/Shared/grouped_models", "/Shared/grouped_experiments", "b2784999872w")

#   The function references an existing parent MLflow run and creates child runs under the parent.
#   """
#   group_name = group_data["group_name"].loc[0] 

#   mlflow.set_experiment(experiment_location)

#   with mlflow.start_run(run_id=run_id) as parent_run:

#     with mlflow.start_run(run_name=group_name, nested=True) as child_run:

#       group_features = group_data.drop(["group_name", "target"], axis=1)
#       group_target = group_data['target']
#       group_model = LinearRegression().fit(group_features.to_numpy(), np.array(group_target))
#       saved_model_name = f"{dbfs_file_path}/{group_name}.pkl"
#       joblib.dump(group_model, saved_model_name)

#       df = pd.DataFrame([group_name], columns=['group_name'])
#       df['model_storage_location'] = saved_model_name

#       params = {"group_name": group_name}
#       mlflow.log_params(params)
#       mlflow.log_metric(key="random_rmse", value=np.random.rand(1))

#   return df

# COMMAND ----------

import time
import mlflow
from mlflow.exceptions import MlflowException

def fit_group_models_udf(dbfs_file_path, experiment_location, run_id, group_data: pd.DataFrame):
  """
  This funcion is intended to be used with functools library to pass in
  parameters other than the Spark DataFrame on which it is applied.

  Example: pandas_udf = functools.partial(fit_group_models_udf, "/dbfs/Shared/grouped_models", "/Shared/grouped_experiments", "b2784999872w")

  The function references an existing parent MLflow run and creates child runs under the parent.
  """
  
  @backoff(max_retries=10, factor=2)
  def log_nested_run(params, metrics):
    with mlflow.start_run(run_name=params['group_name'], nested=True) as child_run:
      mlflow.log_params(params)
      for key, value in metrics.items():
        mlflow.log_metric(key=key, value=value)

  
  # def retry_with_exponential_backoff(func, max_attempts=5, initial_delay=1):
  #   """
  #   Attempts to call the given function with exponential backoff.

  #   Parameters:
  #   - func: The function to call.
  #   - max_attempts: Maximum number of attempts.
  #   - initial_delay: Initial delay between attempts in seconds.
  #   """
  #   attempt = 0
  #   delay = initial_delay
  #   while attempt < max_attempts:
  #       try:
  #           return func()
  #       except MlflowException as e:
  #           print(f"Attempt {attempt+1} failed with error: {e}. Retrying in {delay} seconds...")
  #           time.sleep(delay)
  #           attempt += 1
  #           delay *= 2  # Double the delay for the next attempt
  #   raise Exception("Maximum retry attempts reached. Operation failed.")
  
  mlflow.set_experiment(experiment_location)

  with mlflow.start_run(run_id=run_id) as parent_run:
    group_name = group_data["group_name"].loc[0] 

    group_features = group_data.drop(["group_name", "target"], axis=1)
    group_target = group_data['target']
    group_model = LinearRegression().fit(group_features.to_numpy(), np.array(group_target))
    saved_model_name = f"{dbfs_file_path}/{group_name}.pkl"
    joblib.dump(group_model, saved_model_name)

    df = pd.DataFrame([group_name], columns=['group_name'])
    df['model_storage_location'] = saved_model_name

    params = {"group_name": group_name}
    metrics = {"random_rmse": np.random.rand(1)}
    
    log_nested_run(params, metrics)

  return df

# COMMAND ----------

# MAGIC %md #### Configure the custom MLflow model

# COMMAND ----------

class ModelLoader(mlflow.pyfunc.PythonModel):
  """
  A custom MLflow model that applies group-level forecasting
  models to each sku contained within an inference dataset.
  """

  def load_context(self, context):
    """
    Load all the models into a Python dictionary
    """
    import joblib

    self.group_models = {}
    for group_name, artifact_path in context.artifacts.items():
      self.group_models[group_name] = joblib.load(artifact_path)
  
  def predict(self, context, group_data):
    """
    Apply the appropirate model to each row.
    """
    predictions = []
    for index, row in group_data.iterrows():
      group_name = (row['group_name'])
      group_features = row.drop(["group_name"]).to_numpy()

      model = self.group_models.get(group_name)

      prediction = model.predict([group_features])
      predictions.append(prediction)
    return pd.DataFrame(predictions, columns=['predictions'])

# COMMAND ----------

experiment_location = '/Users/jon.cheung@databricks.com/nested-runs'
run_name = "group_models_class"
pandas_udf = fit_group_models_udf
pandas_udf_schema = StructType().add('group_name', StringType()).add('model_storage_location', StringType())
custom_mlflow_model = ModelLoader()
temp_model_storage_path = "dbfs:/Users/jon.cheung@databricks.com/composite_model_nested"
parent_model_parmeters = {"algorithm": "random_forest",
                          "num_groups": 10}
delta_location = "main.default.mlc_group_nested_models"


class LogNestedModels():

  def __init__(self, experiment_location, run_name, pandas_udf, pandas_udf_schema, 
               custom_mlflow_model, temp_model_storage_path, parent_model_parmeters, delta_location):

    self.experiment_location = experiment_location
    self.run_name = run_name
    self.pandas_udf = pandas_udf
    self.pandas_udf_schema = pandas_udf_schema
    self.custom_mlflow_model = custom_mlflow_model
    self.temp_model_storage_path = temp_model_storage_path
    self.parent_model_parameters = parent_model_parmeters
    self.delta_location = delta_location

    dbutils.fs.rm(self.temp_model_storage_path, recurse=True)
    dbutils.fs.mkdirs(self.temp_model_storage_path)


  def create_parent_run(self):

    mlflow.set_experiment(self.experiment_location)
    with mlflow.start_run(run_name=self.run_name) as run:
      self.experiment_id = run.info.experiment_id
      self.parent_run_id = run.info.run_id
      mlflow.log_params(self.parent_model_parameters)


  def log_child_runs(self, group_features):

    self.input_example = group_features.limit(5).drop('target').toPandas()

    pandas_udf = partial(self.pandas_udf, 
                         self.temp_model_storage_path.replace("dbfs:", "/dbfs"),
                         self.experiment_location,
                         self.parent_run_id)

    fitted_models = group_features.groupBy('group_name').applyInPandas(pandas_udf, schema=self.pandas_udf_schema)

    fitted_models.write.mode("overwrite").format("delta").option("overwriteSchema", "true").saveAsTable(self.delta_location)

    return spark.table(self.delta_location)
  

  def log_parent_model(self):

    self.artifacts = {}
    model_artifacts = dbutils.fs.ls(self.temp_model_storage_path)
    for artifact in model_artifacts:
      path = artifact.path
      path = f"/{path.replace(':', '')}"
      group_name = path.split('/')[-1].split('.')[0]
      self.artifacts[group_name] = path

    mlflow.set_experiment(self.experiment_location)
    with mlflow.start_run(run_id=self.parent_run_id) as parent_run:

      mlflow.pyfunc.log_model(artifact_path="model", 
                              python_model=self.custom_mlflow_model, 
                              artifacts=self.artifacts,
                              input_example=self.input_example)

  
  def train(self, grouped_data):
    self.create_parent_run()
    self.log_child_runs(features)
    self.log_parent_model()

# COMMAND ----------

group_trainer = LogNestedModels(experiment_location, 
                                run_name, 
                                pandas_udf, 
                                pandas_udf_schema, 
                                custom_mlflow_model, 
                                temp_model_storage_path, 
                                parent_model_parmeters, 
                                delta_location)

group_trainer.train(features)

# COMMAND ----------

logged_model = f"runs:/{group_trainer.parent_run_id}/model"

loaded_model = mlflow.pyfunc.spark_udf(spark, model_uri=logged_model)

features_for_inference = features.drop('target')
predictions = features_for_inference.withColumn('predictions', loaded_model(struct(*map(col, features_for_inference.columns))))

display(predictions)
