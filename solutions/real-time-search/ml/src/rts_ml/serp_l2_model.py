"""The logged SERP L2 models: crosses + a scorer in one pyfunc, so the endpoint scores exactly like training.

`SerpRanker` (DCNv2) is the served model; `SerpLgbmRanker` (LightGBM on the same matrix) is the comparator, served on
CPU only to compare end-to-end latency. Logged with `code_paths` for `rts_ml` and `rts_shared`. The DCNv2 module is
pickled on the CPU and moved to the GPU on load when the endpoint has one.
"""

from __future__ import annotations

import mlflow.pyfunc


class SerpRanker(mlflow.pyfunc.PythonModel):
    def __init__(self, model):
        self.model = model

    def load_context(self, context):
        import torch

        if torch.cuda.is_available():
            self.model = self.model.to("cuda")

    def predict(self, context, model_input, params=None):
        from rts_ml.serp_l2_dcn import score

        return score(self.model, model_input)


class SerpLgbmRanker(mlflow.pyfunc.PythonModel):
    def __init__(self, ranker):
        self.ranker = ranker

    def predict(self, context, model_input, params=None):
        from rts_ml.serp_l2 import model_matrix

        return self.ranker.predict(model_matrix(model_input))
