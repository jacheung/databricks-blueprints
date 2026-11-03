"""Contract: the L1 retrieval outputs the app reads. Owned by ml.

QAC: `qac_prefix_completions` in Delta, synced to Lakebase; the app reads every rank for one prefix, keyed and
filtered by the functions below so build and lookup can't drift.
SERP: the AI Search hybrid index over products; the app requests `SERP_INDEX_COLUMNS`.
"""

from __future__ import annotations

import re

# Per-completion stats from the train split, carried on every prefix row for L2.
QAC_COMPLETION_FEATURES = ("pop_30d", "pop_7d", "ctr", "category", "category_share", "brand", "brand_share",
                           "median_clicked_price")
QAC_PREFIX_PK = ("prefix", "rank")
QAC_PREFIX_COLUMNS = (*QAC_PREFIX_PK, "completion", "prefix_len", "n_tokens", "is_word_boundary",
                      *QAC_COMPLETION_FEATURES)
# What the QAC L2 endpoint takes per candidate (its RequestSource features): the L1 row, `rank` sent as `l1_rank`.
QAC_L2_CANDIDATE_COLUMNS = ("l1_rank", "prefix_len", "n_tokens", "is_word_boundary", *QAC_COMPLETION_FEATURES)
# Every request field the model reads. The endpoint strips the lookup keys before the model, so being a guest is sent
# as its own field rather than read from `user_id`.
QAC_L2_REQUEST_COLUMNS = (*QAC_L2_CANDIDATE_COLUMNS, "is_guest")

SERP_INDEX_PK = "product_id"
SERP_INDEX_TEXT = "search_text"    # title + brand; embedded and keyword-matched, not returned
SERP_INDEX_COLUMNS = (SERP_INDEX_PK, "title", "brand", "category", "price")


# Prefix keys: the table build normalizes completions with `normalize`; the app looks up `lookup_prefix(typed)`.
def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def lookup_prefix(typed: str, max_len: int) -> str:
    """Key for what the user typed: normalized like the completions, but a trailing space is kept."""
    key = re.sub(r"\s+", " ", typed.lower()).lstrip()
    return key[:max_len]


def filter_completions(rows: list[dict], typed: str) -> list[dict]:
    """Input longer than the key: keep completions that start with everything typed."""
    full = re.sub(r"\s+", " ", typed.lower()).lstrip()
    return [r for r in rows if r["completion"].startswith(full)]


def qac_l2_records(rows: list[dict], session_id: str, user_id: str | None) -> list[dict]:
    """QAC L2 endpoint `dataframe_records` for L1 rows: lookup keys plus each row's candidate fields."""
    return [{"session_id": session_id, "user_id": user_id, "l1_rank": r["rank"],
             **{c: r[c] for c in QAC_L2_CANDIDATE_COLUMNS[1:]}, "is_guest": int(user_id is None)} for r in rows]
