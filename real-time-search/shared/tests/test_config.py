import copy
import json
from pathlib import Path

import pytest
import yaml

from rts_shared.config import full_name, load_config, model_name, resolve_auto, table, volume_path

ROOT = Path(__file__).resolve().parents[2]  # real-time-search/
EXAMPLE = ROOT / "shared" / "configs" / "example.yml"


@pytest.fixture
def cfg():
    return load_config(str(EXAMPLE))


def write(tmp_path, cfg):
    path = tmp_path / "c.yml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def test_example_config_loads(cfg):
    assert cfg["unity_catalog"]["catalog"]
    assert set(cfg["models"]) == {"qac_l2", "serp_l2", "query_embedding"}


def test_names_are_three_part(cfg):
    uc = cfg["unity_catalog"]
    assert full_name(cfg, "x") == f"{uc['catalog']}.{uc['schema']}.x"
    assert table(cfg, "session_events") == full_name(cfg, cfg["tables"]["session_events"])
    assert model_name(cfg, "qac_l2") == full_name(cfg, cfg["models"]["qac_l2"]["registered_name"])
    assert volume_path(cfg) == f"/Volumes/{uc['catalog']}/{uc['schema']}/{uc['volume']}"


def test_unknown_table_key_fails(cfg):
    with pytest.raises(KeyError):
        table(cfg, "nope")


def test_missing_section_fails(tmp_path, cfg):
    bad = copy.deepcopy(cfg)
    del bad["feature_store"]
    with pytest.raises(ValueError, match="feature_store"):
        load_config(write(tmp_path, bad))


@pytest.mark.parametrize("name", ["rts_online_store", "RTS-store", "-rts"])
def test_online_store_name_must_be_dns_safe(tmp_path, cfg, name):
    bad = copy.deepcopy(cfg)
    bad["feature_store"]["online_store"] = name
    with pytest.raises(ValueError, match="online_store"):
        load_config(write(tmp_path, bad))


def test_l2_timeout_must_fit_budget(tmp_path, cfg):
    bad = copy.deepcopy(cfg)
    bad["app"]["timeouts_ms"]["qac_l2"] = bad["budgets_ms"]["qac_total_p95"]
    with pytest.raises(ValueError, match="qac_l2"):
        load_config(write(tmp_path, bad))


def test_resolve_auto_fills_only_auto_values(cfg):
    resolved = resolve_auto(cfg, user="me@x.com")
    assert resolved["mlflow"]["experiment"] == "/Users/me@x.com/real-time-search/experiments"
    assert cfg["mlflow"]["experiment"] == "auto"  # input not mutated

    pinned = copy.deepcopy(cfg)
    pinned["mlflow"]["experiment"] = "/Shared/rts"
    assert resolve_auto(pinned, "me@x.com")["mlflow"]["experiment"] == "/Shared/rts"


def test_category_slugs_are_unique(cfg):
    from rts_shared.config import category_slug
    cats = cfg["data"]["categories"]
    assert len({category_slug(c) for c in cats}) == len(cats)


def test_category_medians_and_classify_labels(cfg):
    from rts_shared.config import category_medians, classify_labels
    medians = category_medians(cfg)
    assert medians["Camera & Photo"] == 150
    labels = json.loads(classify_labels(cfg))
    assert list(labels) == list(cfg["data"]["categories"])
    assert labels["Camera & Photo"].startswith("Cameras, lenses")


def test_category_needs_price_and_description(tmp_path, cfg):
    bad = copy.deepcopy(cfg)
    del bad["data"]["categories"]["Baby"]["description"]
    with pytest.raises(ValueError, match="Baby"):
        load_config(write(tmp_path, bad))


def test_sql_string_escapes_quotes_and_backslashes():
    from rts_shared.config import sql_string
    assert sql_string("kids' toys") == "'kids\\' toys'"
    assert sql_string('a "b" \\ c') == "'a \"b\" \\\\ c'"


def test_sim_dates_end_yesterday_relative_to_the_run(cfg):
    from datetime import date
    from rts_shared.config import sim_dates
    sim = cfg["data"]["sim"]
    dates = sim_dates(cfg, today=date(2026, 10, 29))
    assert dates["end_date"] == "2026-10-28"
    assert dates["start_date"] == "2026-09-14"          # history_days (45) days, inclusive
    assert dates["test_split_after"] == "2026-10-15"    # last test_days (14) days are the test set
    assert sim["history_days"] == 45 and sim["test_days"] == 14


def test_test_days_must_leave_training_history(tmp_path, cfg):
    bad = copy.deepcopy(cfg)
    bad["data"]["sim"]["test_days"] = bad["data"]["sim"]["history_days"]
    with pytest.raises(ValueError, match="test_days"):
        load_config(write(tmp_path, bad))


def test_every_table_key_used_by_a_notebook_is_in_the_config(cfg):
    import re
    used = set()
    for notebook in ROOT.glob("*/notebooks/*.py"):
        used |= set(re.findall(r'table\(CFG, "(\w+)"\)', notebook.read_text()))
    assert used, "no table(CFG, ...) calls found"
    assert used <= set(cfg["tables"]), f"missing from config tables: {sorted(used - set(cfg['tables']))}"


def test_serp_l2_training_settings(cfg):
    s = cfg["models"]["serp_l2"]
    assert s["candidates"] == cfg["ai_search"]["serp_l1_k"]  # L2 reranks everything L1 returns
    assert s["train_searches"] > s["eval_searches"] > 0 and s["valid_days"] > 0
    assert str(s["ai_env"]) == "6"  # UCDataset (Beta) runs on AI environment v6


def test_l2_endpoints_are_route_optimized(cfg):
    assert all(cfg["models"][m]["route_optimized"] is True for m in ("qac_l2", "serp_l2", "query_embedding"))


def test_kafka_section(cfg):
    k = cfg["kafka"]
    assert set(k) >= {"connection", "service_credential", "bootstrap_servers", "topic", "partitions", "stream"}
    assert "session_events_ingest" in cfg["tables"]


@pytest.mark.parametrize("topic", ["bad topic", "", "x" * 250])
def test_kafka_topic_name_is_validated(tmp_path, cfg, topic):
    bad = copy.deepcopy(cfg)
    bad["kafka"]["topic"] = topic
    with pytest.raises(ValueError, match="kafka.topic"):
        load_config(write(tmp_path, bad))


def test_serp_l2_lightgbm_comparator_serves_on_cpu(cfg):
    lgbm = cfg["models"]["serp_l2"]["lgbm"]
    assert lgbm["workload_type"] == "CPU" and lgbm["endpoint"] != cfg["models"]["serp_l2"]["endpoint"]
    assert lgbm["registered_name"] != cfg["models"]["serp_l2"]["registered_name"]
