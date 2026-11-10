import pytest

from rts_ml.serp import clean_title, recall_at_k, search_text


def test_clean_title_strips_html_and_entities():
    assert clean_title("Anker <b>PowerCore</b>&nbsp;10000 &amp; Cable") == "Anker PowerCore 10000 & Cable"
    assert clean_title("  Sony   WH-1000XM4\n") == "Sony WH-1000XM4"
    assert clean_title(None) == ""


def test_brand_is_added_only_when_the_title_lacks_it():
    assert search_text("Sony WH-1000XM4 Headphones", "SONY") == "Sony WH-1000XM4 Headphones"
    assert search_text("Wireless Headphones", "Sony") == "Wireless Headphones Sony"
    assert search_text("Wireless Headphones", None) == "Wireless Headphones"
    assert search_text("Wireless Headphones", "  ") == "Wireless Headphones"
    # Whole-word match: "LG" inside "Algae" is not the brand.
    assert search_text("Algae Wafers", "LG") == "Algae Wafers LG"


def test_recall_at_k_is_the_share_of_relevant_items_in_the_top_k():
    retrieved = ["a", "x", "b", "y", "c"]
    assert recall_at_k(retrieved, {"a", "b", "c", "d"}, ks=(1, 3, 5, 10)) == {1: 0.25, 3: 0.5, 5: 0.75, 10: 0.75}


def test_recall_at_k_needs_relevant_items():
    with pytest.raises(ValueError):
        recall_at_k(["a"], set(), ks=(1,))
