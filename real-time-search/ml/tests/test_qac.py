import pytest

from rts_ml.qac import drop_dead_ends, expand_prefixes
from rts_shared.retrieval import QAC_PREFIX_COLUMNS


def stats(query, pop_30d, **kw):
    return {"completion": query, "pop_30d": pop_30d, "pop_7d": kw.get("pop_7d", 0), "ctr": kw.get("ctr", 0.5),
            "category": kw.get("category", "Camera & Photo"), "category_share": kw.get("category_share", 1.0),
            "brand": kw.get("brand", "canon"), "brand_share": kw.get("brand_share", 0.5),
            "median_clicked_price": kw.get("median_clicked_price", 100.0)}


STATS = [stats("camera", 50), stats("camera bag", 30), stats("cat food", 40, category="Pet Supplies"),
         stats("cables", 10), stats("casual shirts", 20, category="Clothing, Shoes & Jewelry")]


def table(rows):
    out = {}
    for r in rows:
        out.setdefault(r["prefix"], []).append(r)
    return out


def test_every_prefix_up_to_max_len_is_a_key():
    rows = table(expand_prefixes([stats("camera bag", 1)], max_len=4, k=50))
    assert sorted(rows) == ["c", "ca", "cam", "came"]
    rows = table(expand_prefixes([stats("cat", 1)], max_len=12, k=50))
    assert sorted(rows) == ["c", "ca", "cat"]  # short queries stop at their full length


def test_ranked_by_popularity_then_text_and_capped_at_k():
    rows = table(expand_prefixes(STATS + [stats("cab", 10)], max_len=12, k=4))
    assert [r["completion"] for r in rows["ca"]] == ["camera", "cat food", "camera bag", "casual shirts"]
    assert [r["rank"] for r in rows["ca"]] == [1, 2, 3, 4]
    assert [r["completion"] for r in rows["cam"]] == ["camera", "camera bag"]
    assert [r["completion"] for r in table(expand_prefixes(STATS + [stats("cab", 10)], 12, 50))["cab"]] \
        == ["cab", "cables"]  # ties broken alphabetically


def test_rows_carry_completion_features_and_prefix_features():
    row = next(r for r in expand_prefixes(STATS, max_len=12, k=50) if r["prefix"] == "camera " )
    assert row == {"prefix": "camera ", "rank": 1, "completion": "camera bag", "prefix_len": 7, "n_tokens": 2,
                   "is_word_boundary": True, "pop_30d": 30, "pop_7d": 0, "ctr": 0.5, "category": "Camera & Photo",
                   "category_share": 1.0, "brand": "canon", "brand_share": 0.5, "median_clicked_price": 100.0}


def test_rows_match_the_retrieval_contract():
    assert all(tuple(r) == QAC_PREFIX_COLUMNS for r in expand_prefixes(STATS, max_len=12, k=50))


def test_drop_dead_ends_keeps_completions_at_or_above_min_ctr():
    kept = drop_dead_ends([stats("camera", 50, ctr=0.6), stats("camra", 40, ctr=0.02), stats("cat", 5, ctr=0.1)],
                          min_ctr=0.1)
    # Popular but rarely engaged ("camra") is a dead end; popularity still ranks what is kept.
    assert [s["completion"] for s in kept] == ["camera", "cat"]


def test_word_boundary_means_the_prefix_ends_a_word():
    rows = table(expand_prefixes(STATS, max_len=12, k=50))
    by_completion = {r["completion"]: r["is_word_boundary"] for r in rows["camera"]}
    assert by_completion == {"camera": True, "camera bag": True}
    assert all(not r["is_word_boundary"] for r in rows["cam"])


def test_duplicate_completions_are_rejected():
    with pytest.raises(ValueError, match="camera"):
        expand_prefixes([stats("camera", 1), stats("camera", 2)], max_len=12, k=50)


def test_unnormalized_completions_are_rejected():
    with pytest.raises(ValueError, match="normalize"):
        expand_prefixes([stats("Camera", 1)], max_len=12, k=50)
