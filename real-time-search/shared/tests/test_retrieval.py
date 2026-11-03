from rts_shared.retrieval import (QAC_COMPLETION_FEATURES, QAC_L2_CANDIDATE_COLUMNS, QAC_L2_REQUEST_COLUMNS,
                                  QAC_PREFIX_COLUMNS, QAC_PREFIX_PK, SERP_INDEX_COLUMNS,
                                  SERP_INDEX_PK, SERP_INDEX_TEXT, SERP_L2_CANDIDATE_COLUMNS, SERP_L2_REQUEST_COLUMNS,
                                  filter_completions, lookup_prefix, normalize, qac_l2_records, serp_l2_records)


def test_qac_prefix_table_columns():
    assert QAC_PREFIX_PK == ("prefix", "rank")
    assert QAC_PREFIX_COLUMNS[:2] == QAC_PREFIX_PK
    assert len(QAC_PREFIX_COLUMNS) == len(set(QAC_PREFIX_COLUMNS))
    # L2 reads the completion features straight off the L1 rows, so they are part of the table.
    assert set(QAC_COMPLETION_FEATURES) <= set(QAC_PREFIX_COLUMNS)
    # L2 matches the candidate's category and brand against the session's.
    assert {"category", "category_share", "brand", "brand_share"} <= set(QAC_COMPLETION_FEATURES)
    assert {"completion", "prefix_len", "n_tokens", "is_word_boundary"} <= set(QAC_PREFIX_COLUMNS)


def test_qac_l2_candidate_columns_come_from_the_prefix_row():
    # The app sends each L1 row's fields to the L2 endpoint; `rank` is renamed `l1_rank`, the rest pass as is.
    assert QAC_L2_CANDIDATE_COLUMNS[0] == "l1_rank"
    assert set(QAC_L2_CANDIDATE_COLUMNS[1:]) <= set(QAC_PREFIX_COLUMNS) - {"prefix", "rank", "completion"}
    assert {"category", "brand", *QAC_COMPLETION_FEATURES} <= set(QAC_L2_CANDIDATE_COLUMNS)


def test_serp_index_columns():
    assert SERP_INDEX_COLUMNS[0] == SERP_INDEX_PK == "product_id"
    assert len(SERP_INDEX_COLUMNS) == len(set(SERP_INDEX_COLUMNS))
    # The app shows title/brand/price and L2 keys category features on category; the embedded text isn't returned.
    assert {"title", "brand", "category", "price"} <= set(SERP_INDEX_COLUMNS)
    assert SERP_INDEX_TEXT not in SERP_INDEX_COLUMNS


def test_normalize():
    assert normalize("  Camera   BAGS ") == "camera bags"
    assert normalize("Camera\tBag") == "camera bag"
    assert normalize("") == ""


def test_lookup_prefix_normalizes_and_truncates():
    assert lookup_prefix("  CAM", max_len=12) == "cam"
    assert lookup_prefix("camera bag for sony", max_len=12) == "camera bag f"
    assert lookup_prefix("", max_len=12) == ""
    assert lookup_prefix("camera ", max_len=12) == "camera "  # a typed trailing space is kept


def test_filter_completions_for_input_longer_than_the_key():
    rows = [{"completion": "camera bag for sony"}, {"completion": "camera bag for canon"}]
    assert filter_completions(rows, "Camera bag for s") == [{"completion": "camera bag for sony"}]
    assert filter_completions(rows, "cam") == rows


def test_qac_l2_records_send_keys_and_each_l1_rows_candidate_fields():
    row = {"prefix": "ca", "rank": 2, "completion": "camera bag", "prefix_len": 2, "n_tokens": 2,
           "is_word_boundary": False, "pop_30d": 30, "pop_7d": 4, "ctr": 0.5, "category": "Camera & Photo",
           "category_share": 0.9, "brand": "canon", "brand_share": 0.4, "median_clicked_price": 40.0}
    records = qac_l2_records([row], session_id="s1", user_id=None)
    # The endpoint looks up session and user features by key and strips the keys before the model, so whether the
    # shopper is logged in travels as its own request field.
    assert records == [{"session_id": "s1", "user_id": None, "l1_rank": 2,
                        **{c: row[c] for c in QAC_L2_CANDIDATE_COLUMNS[1:]}, "is_guest": 1}]
    assert qac_l2_records([row], session_id="s1", user_id="u1")[0]["is_guest"] == 0
    assert set(records[0]) == {"session_id", "user_id", *QAC_L2_REQUEST_COLUMNS}


def test_serp_l2_candidate_columns_come_from_the_index_row():
    # The index row's fields, plus where L1 put it; product_id is a lookup key (item features), not a request field.
    assert SERP_L2_CANDIDATE_COLUMNS[:2] == ("l1_rank", "l1_score")
    assert set(SERP_L2_CANDIDATE_COLUMNS[2:]) <= set(SERP_INDEX_COLUMNS)
    assert SERP_INDEX_PK not in SERP_L2_REQUEST_COLUMNS
    assert SERP_L2_REQUEST_COLUMNS == (*SERP_L2_CANDIDATE_COLUMNS, "is_guest")


def test_serp_l2_records_send_keys_and_each_hits_candidate_fields():
    hits = [{"product_id": "B01", "title": "Sony bag", "brand": "sony", "category": "Camera & Photo", "price": 40.0,
             "score": 0.9},
            {"product_id": "B02", "title": "Bag", "brand": None, "category": "Camera & Photo", "price": 12.0,
             "score": 0.7}]
    records = serp_l2_records(hits, session_id="s1", user_id=None)
    # Hits arrive in L1 order; rank is 1-based like the QAC table. The endpoint strips the keys before the model.
    assert records[0] == {"session_id": "s1", "user_id": None, "product_id": "B01", "l1_rank": 1, "l1_score": 0.9,
                          "price": 40.0, "brand": "sony", "category": "Camera & Photo", "is_guest": 1}
    assert records[1]["l1_rank"] == 2 and records[1]["brand"] is None
    assert serp_l2_records(hits, session_id="s1", user_id="u1")[0]["is_guest"] == 0
    assert set(records[0]) == {"session_id", "user_id", SERP_INDEX_PK, *SERP_L2_REQUEST_COLUMNS}
