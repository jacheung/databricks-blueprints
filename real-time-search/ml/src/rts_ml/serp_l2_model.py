"""The logged SERP L2 model: crosses + DCNv2 in one pyfunc, so the endpoint scores exactly like training.

Logged with `code_paths` for `rts_ml` and `rts_shared`. The module is pickled on the CPU and moved to the GPU on
first use when the endpoint has one, so one logged version serves on both the GPU endpoint and the CPU variant.
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
