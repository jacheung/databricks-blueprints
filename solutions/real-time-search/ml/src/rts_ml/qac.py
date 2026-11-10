"""QAC L1: build the prefix -> top-k completions table. Lookups use the same keys (`rts_shared.retrieval`)."""

from __future__ import annotations

import heapq
from collections import defaultdict

from rts_shared.retrieval import QAC_COMPLETION_FEATURES as COMPLETION_FEATURES
from rts_shared.retrieval import normalize


def drop_dead_ends(stats: list[dict], min_ctr: float) -> list[dict]:
    """Popularity counts every submitted search; completions searchers rarely engage with are not served."""
    return [s for s in stats if s["ctr"] >= min_ctr]


def expand_prefixes(stats: list[dict], max_len: int, k: int) -> list[dict]:
    """One row per (prefix, rank) for prefixes of 1..max_len characters, ranked by 30-day popularity."""
    seen = set()
    by_prefix = defaultdict(list)
    for s in stats:
        completion = s["completion"]
        if completion != normalize(completion):
            raise ValueError(f"completion {completion!r} is not normalized; use rts_shared.retrieval.normalize")
        if completion in seen:
            raise ValueError(f"duplicate completion {completion!r}")
        seen.add(completion)
        for n in range(1, min(max_len, len(completion)) + 1):
            by_prefix[completion[:n]].append(s)

    rows = []
    for prefix, candidates in by_prefix.items():
        top = heapq.nsmallest(k, candidates, key=lambda s: (-s["pop_30d"], s["completion"]))
        for rank, s in enumerate(top, start=1):
            completion = s["completion"]
            rows.append({
                "prefix": prefix, "rank": rank, "completion": completion, "prefix_len": len(prefix),
                "n_tokens": len(completion.split()),
                # The prefix ends a word: the user has finished typing a token, not stopped mid-word.
                "is_word_boundary": len(completion) == len(prefix) or completion[len(prefix)] == " "
                                    or prefix.endswith(" "),
                **{f: s[f] for f in COMPLETION_FEATURES},
            })
    return rows
