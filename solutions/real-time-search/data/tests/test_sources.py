import gzip
import hashlib

import pytest

from rts_data.sources import (ESCI_COMMIT, ESCI_FILES, esci_url, read_category_snapshot, sha256_file,
                              write_category_snapshot)

ALLOWED = {"Electronics", "Pet Supplies"}


def test_esci_is_pinned_to_a_commit_with_a_checksum_per_file():
    assert len(ESCI_COMMIT) == 40 and int(ESCI_COMMIT, 16)  # a full SHA, not a branch
    assert set(ESCI_FILES) == {"shopping_queries_dataset_examples.parquet", "shopping_queries_dataset_products.parquet"}
    for name, (sha256, size) in ESCI_FILES.items():
        assert len(sha256) == 64 and size > 0
        assert f"/{ESCI_COMMIT}/" in esci_url(name) and esci_url(name).endswith(name)


def test_sha256_file(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x" * 100_000)
    assert sha256_file(str(path)) == hashlib.sha256(b"x" * 100_000).hexdigest()


def test_snapshot_round_trips_sorted_with_missing_labels_kept(tmp_path):
    path = str(tmp_path / "c.csv.gz")
    write_category_snapshot(path, "product_id", [("b2", "Pet Supplies"), ("a1", "Electronics"), ("c3", None)])
    # None = classified, no answer: kept so a rebuild doesn't ask the LLM again
    assert read_category_snapshot(path, "product_id", ALLOWED) == [("a1", "Electronics"), ("b2", "Pet Supplies"),
                                                                    ("c3", None)]
    assert gzip.open(path, "rt").readline().strip() == "product_id,category"


def test_snapshot_bytes_depend_only_on_the_rows(tmp_path):
    # Re-exporting unchanged labels must not show up as a git diff (gzip stores a timestamp unless told not to).
    a, b = str(tmp_path / "a.csv.gz"), str(tmp_path / "b.csv.gz")
    rows = [("q2", "Electronics"), ("q1", "Pet Supplies")]
    write_category_snapshot(a, "query_id", rows)
    write_category_snapshot(b, "query_id", list(reversed(rows)))
    assert open(a, "rb").read() == open(b, "rb").read()


def test_snapshot_rejects_labels_the_config_no_longer_has(tmp_path):
    path = str(tmp_path / "c.csv.gz")
    write_category_snapshot(path, "query_id", [("q1", "Toys & Games")])
    with pytest.raises(ValueError, match="Toys & Games"):
        read_category_snapshot(path, "query_id", ALLOWED)


def test_snapshot_rejects_the_wrong_key(tmp_path):
    path = str(tmp_path / "c.csv.gz")
    write_category_snapshot(path, "query_id", [("q1", "Electronics")])
    with pytest.raises(ValueError, match="product_id"):
        read_category_snapshot(path, "product_id", ALLOWED)


def test_committed_snapshots_match_the_config_categories():
    # Changing `data.categories` without re-classifying fails here, not mid-rebuild on Databricks.
    from pathlib import Path

    from rts_shared.config import category_medians, load_config
    root = Path(__file__).resolve().parents[2]
    allowed = set(category_medians(load_config(str(root / "shared" / "configs" / "example.yml"))))
    for name, key in (("query_categories.csv.gz", "query_id"), ("product_title_categories.csv.gz", "product_id")):
        rows = read_category_snapshot(str(root / "data" / "snapshots" / name), key, allowed)
        assert rows and len({k for k, _ in rows}) == len(rows), f"{name}: empty or duplicate ids"
