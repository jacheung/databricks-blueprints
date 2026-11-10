# Databricks notebook source
# MAGIC %md ## Synthetic data generation. 
# MAGIC
# MAGIC Generate some features for a regression model

# COMMAND ----------

from pyspark.sql.types import StructType, StructField, StringType, FloatType
from sklearn.datasets import make_regression
from functools import partial
import pandas as pd

# COMMAND ----------

groups = 5000
n_features_per_group = 10
n_samples_per_group = 200

# COMMAND ----------

def create_groups(groups=groups):
  """
  Create a DataFrame of group names, one row per group with a single column
  containing the group name
  """

  groups = [[f'group_{str(n+1).zfill(2)}'] for n in range(groups)]

  schema = StructType()
  schema.add('group_name', StringType())

  return spark.createDataFrame(groups, schema=schema)


def get_feature_col_names(n_features_per_group=n_features_per_group):
  """Create a list of column names for the generated features"""

  return [f"features_{n}" for n in range(n_features_per_group)]

# This is the UDF for creating functions. There's two more parameters compared to a traditional UDF. This is meant to be used with functools.partial. If we didn't do this, the parameters for the UDF would be implicitly defined. 
def create_group_features(n_features_per_group, n_samples_per_group, group_data: pd.DataFrame) -> pd.DataFrame:
  features, target = make_regression(n_samples=n_samples_per_group, n_features=n_features_per_group)
  feature_names = get_feature_col_names()
  df = pd.DataFrame(features, columns=feature_names)

  df['target'] = target.tolist()

  group_name = group_data["group_name"].loc[0]
  df['group_name'] = group_name

  col_order = ['group_name'] + feature_names + ['target']

  return df[col_order]


# COMMAND ----------

# Create a Spark Schema matching the PandasUDF output
spark_schema = StructType()
spark_schema.add('group_name', StringType())
for feature_name in get_feature_col_names():
  spark_schema.add(feature_name, FloatType())
spark_schema.add('target', FloatType())

groups_df = create_groups()

configure_features_udf=partial(create_group_features, n_features_per_group, n_samples_per_group)

features = groups_df.groupBy('group_name').applyInPandas(func=configure_features_udf,
                                                          schema=spark_schema)
