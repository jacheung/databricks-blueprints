from pathlib import Path

from rts_shared.config import load_config
from rts_shared.lakebase import features_database

CONFIGS = Path(__file__).resolve().parents[1] / "configs"


def test_feature_online_tables_live_in_the_database_named_after_the_catalog():
    cfg = load_config(str(CONFIGS / "example.yml"))
    assert features_database(cfg) == cfg["unity_catalog"]["catalog"]
    assert features_database(cfg) != cfg["lakebase"]["database"]  # not where the synced tables are
