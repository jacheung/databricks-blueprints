"""Router model (MLflow models-from-code).

One registered model fronts every per-group model. The models are not in the artifact:
each request row carries `model_json`, which Feature Engineering looks up by `group_name`
(online store at serving time, Delta feature table in `score_batch`). Nothing here is
model-specific; deserializing and forecasting come from forecasting.py.
"""

from __future__ import annotations

from functools import lru_cache

import mlflow
import pandas as pd
from mlflow.types.schema import Array, ColSpec, DataType, Schema

from forecasting import deserialize_model, forecast_from_model

OUTPUT_COLUMNS = ["group_name", "ds", "yhat", "yhat_lower", "yhat_upper"]
# UC registration needs an output schema; FE can't infer one without a label.
OUTPUT_SCHEMA = Schema([ColSpec(DataType.string, "group_name"), ColSpec(Array(DataType.string), "ds")]
                       + [ColSpec(Array(DataType.double), c) for c in ("yhat", "yhat_lower", "yhat_upper")])


@lru_cache(maxsize=256)
def load_model(model_json: str):
    return deserialize_model(model_json)


class ProphetRouter(mlflow.pyfunc.PythonModel):
    def predict(self, context, model_input: pd.DataFrame, params=None) -> pd.DataFrame:
        rows = []
        for group_name, horizon, model_json in model_input[["group_name", "horizon", "model_json"]].itertuples(index=False):
            if not isinstance(model_json, str):
                raise ValueError(f"No saved model for group_name={group_name!r}")
            rows.append({"group_name": group_name, **forecast_from_model(load_model(model_json), int(horizon))})
        return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


mlflow.models.set_model(ProphetRouter())
