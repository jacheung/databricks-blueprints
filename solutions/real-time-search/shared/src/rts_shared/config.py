"""Helpers shared by the notebooks, the app, and the tests: config loading and resource names."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, timedelta

import yaml

REQUIRED_SECTIONS = ("workspace", "unity_catalog", "data", "tables", "kafka", "feature_store", "lakebase",
                     "ai_search", "models", "app", "budgets_ms", "mlflow", "cleanup")
DNS_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
KAFKA_TOPIC = re.compile(r"^[a-zA-Z0-9._-]{1,249}$")


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        raise ValueError(f"config is missing sections: {', '.join(missing)}")
    # Lakebase instance names must be DNS-compliant; underscores fail at create time.
    if not DNS_NAME.match(cfg["feature_store"]["online_store"]):
        raise ValueError("feature_store.online_store must be lowercase letters, digits and hyphens")
    for category, spec in cfg["data"]["categories"].items():
        if not spec.get("median_price") or not spec.get("description"):
            raise ValueError(f"data.categories.{category} needs median_price and description")
    if not KAFKA_TOPIC.match(cfg["kafka"]["topic"] or ""):
        raise ValueError("kafka.topic must be 1-249 characters of letters, digits, '.', '_' or '-'")
    sim = cfg["data"]["sim"]
    if not 0 < sim["test_days"] < sim["history_days"]:
        raise ValueError("data.sim.test_days must be positive and below history_days")
    for surface in ("qac", "serp"):
        if cfg["app"]["timeouts_ms"][f"{surface}_l2"] >= cfg["budgets_ms"][f"{surface}_total_p95"]:
            raise ValueError(f"app.timeouts_ms.{surface}_l2 must be below budgets_ms.{surface}_total_p95")
    for p in cfg["app"].get("demo_personas", []):
        if p.get("category") not in cfg["data"]["categories"]:
            raise ValueError(f"app.demo_personas: {p.get('name')} needs a category from data.categories")
    return cfg


def full_name(cfg: dict, name: str) -> str:
    uc = cfg["unity_catalog"]
    return f"{uc['catalog']}.{uc['schema']}.{name}"


def table(cfg: dict, key: str) -> str:
    return full_name(cfg, cfg["tables"][key])


def model_name(cfg: dict, key: str) -> str:
    return full_name(cfg, cfg["models"][key]["registered_name"])


def volume_path(cfg: dict) -> str:
    uc = cfg["unity_catalog"]
    return f"/Volumes/{uc['catalog']}/{uc['schema']}/{uc['volume']}"


def resolve_auto(cfg: dict, user: str) -> dict:
    """Fill `auto` values from the live workspace; returns a copy."""
    cfg = copy.deepcopy(cfg)
    if cfg["mlflow"]["experiment"] == "auto":
        cfg["mlflow"]["experiment"] = f"/Users/{user}/real-time-search/experiments"
    return cfg


def category_slug(category: str) -> str:
    """Category label as a column-name suffix, e.g. 'Camera & Photo' -> 'camera_photo'."""
    return re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")


def category_medians(cfg: dict) -> dict:
    return {c: spec["median_price"] for c, spec in cfg["data"]["categories"].items()}


def classify_labels(cfg: dict) -> str:
    """ai_classify labels as JSON {label: description}, in config order."""
    return json.dumps({c: spec["description"] for c, spec in cfg["data"]["categories"].items()})


def sql_string(text: str) -> str:
    """Quote text as a Spark SQL string literal."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sim_dates(cfg: dict, today: date) -> dict:
    """Simulated history ends yesterday, so live online user/item features have data in their windows."""
    sim = cfg["data"]["sim"]
    end = today - timedelta(days=1)
    return {"start_date": (end - timedelta(days=sim["history_days"] - 1)).isoformat(),
            "end_date": end.isoformat(),
            "test_split_after": (end - timedelta(days=sim["test_days"] - 1)).isoformat()}


def shift_days(cfg: dict, first_event_date: date, today: date) -> int:
    """Whole days to move the simulated history so it ends yesterday again, as a fresh clickstream_sim run would."""
    days = (date.fromisoformat(sim_dates(cfg, today)["start_date"]) - first_event_date).days
    if days < 0:
        raise ValueError(f"simulated history starts {-days} days ahead of sim_dates; not shifting it backwards")
    return days


def window_strength(daily: list[tuple[date, float]]) -> dict:
    """A Count feature's per-day totals from its offline table, (window-end date, sum). Every full window holds about the
    same total, so a stale history shows as the latest window falling below the best day."""
    latest, best = max(daily), max(s for _, s in daily)
    return {"latest_date": latest[0].isoformat(), "latest": int(latest[1]), "best": int(best),
            "ratio": round(latest[1] / best, 3)}
