# Bring your own model

The notebooks, the feature table, the router and the endpoint don't depend on Prophet. To use
your own per-group model, replace four functions in `lib/forecasting.py` and the pins in
`lib/requirements.txt`. Everything else runs on top of them.

This harness is for **local** models: one independent model per group. A single global model
trained across all groups (a deep-learning model, say) doesn't fit this shape; train and register
it as one ordinary model instead.

## What you replace

| In | Replace | Contract |
|---|---|---|
| `lib/forecasting.py` | `_fit(pdf)` | Takes one group's rows (`ds`, `group_name`, `y`, plus any other source columns) and returns a fitted model |
| | `_forecast(model, horizon)` | Returns `horizon` rows with `ds`, `yhat`, `yhat_lower`, `yhat_upper`, starting one step after the training data |
| | `serialize_model(model)` | Returns the string stored in `model_json`. Keep it small: drop anything forecasting doesn't read |
| | `deserialize_model(model_json)` | Inverse of `serialize_model` |
| | `FREQ` | Your data's frequency |
| `lib/requirements.txt` | Pinned model libraries | Installed by every notebook and packaged with the router, so the endpoint has them |
| `resources/*.yml` | `dependencies` | Only if you run the paths as bundle jobs: these pins are separate from `requirements.txt` |

## What you get without changes

Built on those four functions:

- `train_and_forecast` (path 1), `backtest` (path 1), `train_to_json` (paths 2 and 3)
- `forecast_from_json`, used to check the endpoint against the stored model
- `ProphetRouter` in `lib/router_model.py`: it calls only `deserialize_model` and
  `_forecast`. The class name is a label; so are `prophet_models`, `prophet_router` and the
  endpoint name.

## How the router handles model parameters

The router is a single registered model that serves every group. It gets parameters from three
places:

| Kind | Example | Where it lives | What you change |
|---|---|---|---|
| Fitted state and per-group hyperparameters | Prophet's coefficients; a `changepoint_prior_scale` tuned per group | Inside the `model_json` string | Nothing: `serialize_model` writes them, `deserialize_model` restores them |
| Request-time parameters | `horizon` | A column in the request | To add one, add the column to `keys` in notebooks 02 and 03 (the router's input signature comes from that DataFrame) and read it in `ProphetRouter.predict` |
| Per-group metadata outside the model | Model type for mixed model families, model version | Extra columns in `prophet_models` | Add them to `MODEL_SCHEMA` and `train_to_json`, then to `feature_names` in the `FeatureLookup` in notebooks 02 and 03 |

A request row reaches `ProphetRouter.predict` as `group_name`, `horizon` and `model_json`.
Feature Engineering adds `model_json` from the Delta table in `fe.score_batch` and from the
Lakebase online table at the endpoint. The router deserializes it through an LRU cache keyed by
the string, so a model is parsed once per replica until it's evicted or retrained.

## Changes that reach past `forecasting.py`

- **No prediction intervals.** `yhat_lower` and `yhat_upper` appear in `FORECAST_SCHEMA`, the
  router's `OUTPUT_SCHEMA`, the `result_type` in notebook 02, and `coverage` in `backtest`.
  Change them together, or return `yhat` for both bounds.
- **Binary model formats** (pickles, booster files). `model_json` is a string column, so
  base64-encode in `serialize_model` and decode in `deserialize_model`. Size matters: every row
  is synced to Lakebase, and the slim Prophet JSON here is 5–8 KB.
- **Exogenous regressors** (price, promotions). Training works if the source table carries the
  columns and `_fit` reads them. Forecasting needs their *future* values: path 1 needs a table of
  future values joined in, and in paths 2 and 3 the request has to carry them (a request-time
  parameter, as above). The harness doesn't do this yet.

## Check your model

1. `pytest tests`. The contract tests (shape, round trip, router routing and caching, backtest)
   should pass unchanged. `test_saved_json_is_slim_and_forecasts_like_full_model` and
   `test_requirements_file_pins_the_installed_prophet` are Prophet-specific; rewrite or delete
   them.
2. Run `01_no_save_forecast` on a few groups and read the backtest.
3. Run `02_save_offline_inference`, then `03_save_online_inference`. Path 3 checks that the
   endpoint's forecast matches the stored model.
