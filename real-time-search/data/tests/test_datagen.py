import statistics

import pytest

from rts_data.datagen import ESCI_GAIN, category_vote, synthetic_price
from rts_shared.config import category_slug


def test_esci_gain_is_standard():
    assert ESCI_GAIN == {"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0}


def test_category_slug_is_a_safe_column_suffix():
    assert category_slug("Clothing, Shoes & Jewelry") == "clothing_shoes_jewelry"
    assert category_slug("Camera & Photo") == "camera_photo"


def test_price_is_deterministic_per_product():
    assert synthetic_price("B001", 80.0, 0.6, seed=42) == synthetic_price("B001", 80.0, 0.6, seed=42)
    assert synthetic_price("B001", 80.0, 0.6, seed=42) != synthetic_price("B002", 80.0, 0.6, seed=42)
    assert synthetic_price("B001", 80.0, 0.6, seed=42) != synthetic_price("B001", 80.0, 0.6, seed=7)


def test_price_centres_on_category_median():
    prices = [synthetic_price(f"P{i}", 80.0, 0.6, seed=42) for i in range(5000)]
    assert 70 < statistics.median(prices) < 90
    assert min(prices) >= 1.0
    assert all(round(p, 2) == p for p in prices)


def test_vote_prefers_exact_over_substitute():
    assert category_vote([("Electronics", "S"), ("Electronics", "S"), ("Camera & Photo", "E")]) == "Camera & Photo"
    assert category_vote([("Electronics", "S"), ("Electronics", "S"), ("Electronics", "S"),
                          ("Camera & Photo", "E")]) == "Electronics"


def test_vote_ignores_complement_and_irrelevant():
    assert category_vote([("Pet Supplies", "C"), ("Baby", "I")]) is None
    assert category_vote([("Pet Supplies", "C"), ("Baby", "E")]) == "Baby"


def test_vote_ties_break_alphabetically():
    assert category_vote([("Toys & Games", "E"), ("Baby", "E")]) == "Baby"
