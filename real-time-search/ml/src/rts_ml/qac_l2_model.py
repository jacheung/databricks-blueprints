"""The logged QAC L2 model: crosses + LightGBM in one pyfunc, so the endpoint scores exactly like training.

Kept apart from `rts_ml.qac_l2` so the training logic imports without MLflow. Logged with `code_paths` for
`rts_ml` and `rts_shared`, which the endpoint imports when it unpickles this class.
"""

from __future__ import annotations

import mlflow.pyfunc

from rts_ml.qac_l2 import score


class QacRanker(mlflow.pyfunc.PythonModel):
    def __init__(self, ranker):
        self.ranker = ranker

    def predict(self, context, model_input, params=None):
        return score(self.ranker, model_input)
