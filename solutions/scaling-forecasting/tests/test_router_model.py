import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from forecasting import make_group_series, train_to_json  # noqa: E402
from router_model import OUTPUT_SCHEMA, ProphetRouter, load_model  # noqa: E402


@pytest.fixture(scope="module")
def models():
    return {g: train_to_json(make_group_series(g, n_rows=300, seed=i)).loc[0, "model_json"]
            for i, g in enumerate(["group_01", "group_02"])}


def test_routes_each_row_to_its_own_model(models):
    req = pd.DataFrame({"group_name": ["group_01", "group_02"],
                        "horizon": [3, 5],
                        "model_json": [models["group_01"], models["group_02"]]})
    out = ProphetRouter().predict(None, req)
    assert len(out) == 2
    assert list(out.columns) == ["group_name", "ds", "yhat", "yhat_lower", "yhat_upper"]
    assert out["group_name"].tolist() == ["group_01", "group_02"]
    assert [len(x) for x in out["yhat"]] == [3, 5]
    assert out.loc[0, "yhat"] != out.loc[1, "yhat"][:3]


def test_reuses_deserialized_model(models):
    load_model.cache_clear()
    req = pd.DataFrame({"group_name": ["group_01"] * 3, "horizon": [2] * 3,
                        "model_json": [models["group_01"]] * 3})
    ProphetRouter().predict(None, req)
    info = load_model.cache_info()
    assert info.misses == 1 and info.hits == 2


def test_declared_output_schema_matches_predict_output(models):
    from mlflow.models import infer_signature

    req = pd.DataFrame({"group_name": ["group_01"], "horizon": [2], "model_json": [models["group_01"]]})
    out = ProphetRouter().predict(None, req)
    assert infer_signature(req, out).outputs == OUTPUT_SCHEMA


def test_router_deserializes_through_forecasting(models, monkeypatch):
    """The router has no model-specific code: swapping the model means editing forecasting.py."""
    import forecasting
    import router_model

    calls = []
    real = forecasting.deserialize_model
    monkeypatch.setattr(router_model, "deserialize_model", lambda s: calls.append(s) or real(s))
    load_model.cache_clear()
    load_model(models["group_01"])
    assert calls == [models["group_01"]]


def test_missing_model_raises_with_group_name():
    req = pd.DataFrame({"group_name": ["group_99"], "horizon": [2], "model_json": [None]})
    with pytest.raises(ValueError, match="group_99"):
        ProphetRouter().predict(None, req)
