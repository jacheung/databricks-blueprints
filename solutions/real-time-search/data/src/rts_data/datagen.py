"""Pure helpers for building the catalog and simulated sessions; Spark-free so they are unit-tested locally."""

from __future__ import annotations

import hashlib
import itertools
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

# Standard ESCI gains (Exact, Substitute, Complement, Irrelevant) used for NDCG.
ESCI_GAIN = {"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0}
# How much a query's category counts toward a product's category; C/I queries say little about the product.
VOTE_WEIGHT = {"E": 1.0, "S": 0.4}


def _rng(*parts) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def synthetic_price(product_id: str, median: float, sigma: float, seed: int) -> float:
    """ESCI has no prices: draw a lognormal around the category median, fixed per product."""
    price = median * _rng(seed, product_id).lognormvariate(0.0, sigma)
    return round(max(price, 1.0), 2)


def category_vote(votes: list[tuple[str, str]]) -> str | None:
    """Category of a product from (query category, esci_label) pairs; None if no E/S votes."""
    scores = defaultdict(float)
    for category, label in votes:
        scores[category] += VOTE_WEIGHT.get(label, 0.0)
    scores = {c: s for c, s in scores.items() if s > 0}
    if not scores:
        return None
    return min(scores, key=lambda c: (-scores[c], c))


# ---- Session simulator ------------------------------------------------------------------------------------------
# Click model: P(click) = base rate for the ESCI label x how well the product fits the session's intent.
# Base rates are set so roughly half of searches get a click or cart; the rest are abandoned.
CLICK_BASE = {"E": 0.07, "S": 0.03, "C": 0.01, "I": 0.002}
OFF_CATEGORY = 0.2      # intent multiplier for a product outside the intent category
BRAND_BOOST = 2.5       # ... for the intent brand
PRICE_SCALE = 0.7       # price fit = exp(-|log(price / target)| / PRICE_SCALE)
PRICE_TIERS = (0.5, 1.0, 2.0)   # budget / mid / premium, as a multiple of the category median
MAX_CLICKS = 3
HOME_INTENT_SHARE = 0.7  # logged-in intent sessions reuse the user's long-term intent this often
TOP_BRANDS = 20          # intent brands come from each category's most common brands, not the long tail
BRAND_QUERY_SHARE = 0.7  # a brand shopper picks queries where that brand shows up this often


@dataclass(frozen=True)
class Catalog:
    queries: list            # (query_id, query, category)
    judged: dict             # query_id -> [(product_id, esci_label, category, brand, price)]
    medians: dict            # category -> median price
    cum_popularity: list     # cumulative Zipf weight per query, aligned with `queries`
    by_category: dict        # category -> ([query index], [cumulative weight])
    cum_category: list       # (category, cumulative query weight), for drawing an intent
    brands: dict             # category -> ([brand], [cumulative count]), top TOP_BRANDS only
    by_brand: dict           # (category, brand) -> ([query index], [cumulative popularity x brand products])

    @classmethod
    def build(cls, queries, judged, medians):
        # Canonical order: draws pick by position (a query in its pool, one draw per judged product), so input order
        # (Spark row order, which can change between runs) would otherwise change the sessions for the same seed.
        queries = sorted((q for q in queries if q[0] in judged and q[2] in medians), key=lambda q: q[0])
        judged = {qid: sorted(products, key=lambda p: p[0]) for qid, products in sorted(judged.items())}
        # Popularity rank is a fixed shuffle of the queries, so it doesn't depend on input order.
        ranked = sorted(range(len(queries)), key=lambda i: hashlib.sha256(str(queries[i][0]).encode()).digest())
        popularity = [0.0] * len(queries)
        for rank, i in enumerate(ranked, start=1):
            popularity[i] = 1.0 / rank
        by_category, brands = defaultdict(lambda: ([], [])), defaultdict(lambda: defaultdict(int))
        by_brand = defaultdict(lambda: ([], []))
        for i, (qid, _, category) in enumerate(queries):
            by_category[category][0].append(i)
            by_category[category][1].append(popularity[i])
            in_query = defaultdict(int)
            for _, _, p_category, brand, _ in judged[qid]:
                if brand and p_category == category:
                    in_query[brand] += 1
            for brand, n in in_query.items():
                brands[category][brand] += n
                by_brand[(category, brand)][0].append(i)
                by_brand[(category, brand)][1].append(popularity[i] * n)
        brands = {c: dict(sorted(b.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_BRANDS]) for c, b in brands.items()}
        # Cumulative weights once here: rng.choices(weights=...) would rebuild them on every draw.
        cum = lambda w: list(itertools.accumulate(w))
        categories = sorted(by_category)
        return cls(queries=queries, judged=judged, medians=medians, cum_popularity=cum(popularity),
                   by_category={c: (idx, cum(w)) for c, (idx, w) in by_category.items()},
                   cum_category=(categories, cum(sum(by_category[c][1]) for c in categories)),
                   brands={c: (list(b), cum(b.values())) for c, b in brands.items()},
                   by_brand={(c, b): (idx, cum(w)) for (c, b), (idx, w) in by_brand.items() if b in brands.get(c, {})})


@dataclass(frozen=True)
class Intent:
    category: str
    price_target: float
    brand: str | None


def _random_intent(rng, catalog: Catalog) -> Intent:
    categories, cum_weights = catalog.cum_category
    category = rng.choices(categories, cum_weights=cum_weights)[0]
    brand = None
    if category in catalog.brands and rng.random() < 0.6:
        names, cum_counts = catalog.brands[category]
        brand = rng.choices(names, cum_weights=cum_counts)[0]
    return Intent(category, catalog.medians[category] * rng.choice(PRICE_TIERS), brand)


def _fit(intent: Intent | None, category, brand, price) -> float:
    if intent is None:
        return 1.0
    fit = (1.0 if category == intent.category else OFF_CATEGORY)
    fit *= BRAND_BOOST if intent.brand and brand == intent.brand else 1.0
    fit *= 1.5 * math.exp(-abs(math.log(max(price, 0.01) / intent.price_target)) / PRICE_SCALE)
    return fit


def simulate(catalog: Catalog, sim: dict):
    """Yield events session by session. Deterministic for sim['seed']."""
    rng = random.Random(sim["seed"])
    start = datetime.fromisoformat(sim["start_date"])
    span = (datetime.fromisoformat(sim["end_date"]) + timedelta(days=1) - start).total_seconds()
    users = {u: _random_intent(rng, catalog) for u in range(sim["n_users"])}
    all_queries = range(len(catalog.queries))

    for s in range(sim["n_sessions"]):
        user = rng.randrange(sim["n_users"]) if rng.random() < sim["logged_in_share"] else None
        device = "mobile" if rng.random() < sim["mobile_share"] else "desktop"
        intent = None
        if rng.random() < sim["intent_session_share"]:
            intent = users[user] if user is not None and rng.random() < HOME_INTENT_SHARE else _random_intent(rng, catalog)
        session = {"session_id": f"s{s:07d}", "user_id": None if user is None else f"u{user:06d}", "device": device,
                   "intent_category": intent.category if intent else None,
                   "intent_brand": intent.brand if intent else None,
                   "intent_price_target": round(intent.price_target, 2) if intent else None}
        t = start + timedelta(seconds=rng.random() * span)

        n_events = 0

        def emit(event_type, search_id, query_id, query, product_id=None, category=None, brand=None, price=None):
            nonlocal t, n_events
            t += timedelta(seconds=rng.uniform(2, 45))
            n_events += 1
            # Millisecond precision, as in the Kafka payload. Floor the emitted value only, so the clock doesn't drift.
            event_time = t.replace(microsecond=t.microsecond // 1000 * 1000)
            return {**session, "search_id": search_id, "event_id": f"{session['session_id']}-{n_events:03d}",
                    "event_time": event_time, "event_type": event_type,
                    "query_id": query_id, "query": query, "product_id": product_id, "category": category,
                    "brand": brand, "price": price}

        n_searches = 1
        while n_searches < 5 and rng.random() < 0.5:
            n_searches += 1
        for k in range(n_searches):
            if intent is None:
                pool, cum_weights = all_queries, catalog.cum_popularity
            elif intent.brand and rng.random() < BRAND_QUERY_SHARE:
                pool, cum_weights = catalog.by_brand[(intent.category, intent.brand)]
            else:
                pool, cum_weights = catalog.by_category[intent.category]
            query_id, query, query_category = catalog.queries[rng.choices(pool, cum_weights=cum_weights)[0]]
            search_id = f"{session['session_id']}-{k}"
            yield emit("search", search_id, query_id, query, category=query_category)

            clicked = []
            for product_id, label, category, brand, price in catalog.judged[query_id]:
                fit = _fit(intent, category, brand, price)
                if rng.random() < min(0.9, CLICK_BASE[label] * fit):
                    clicked.append((fit, product_id, category, brand, price))
            rng.shuffle(clicked)
            for fit, product_id, category, brand, price in clicked[:MAX_CLICKS]:
                yield emit("click", search_id, query_id, query, product_id, category, brand, price)
                if rng.random() < min(0.6, 0.1 * fit):
                    yield emit("add_to_cart", search_id, query_id, query, product_id, category, brand, price)
                if rng.random() < 0.6:
                    yield emit("back", search_id, query_id, query)


def intent_lift(events, judged: dict) -> dict:
    """Within intent sessions' searches: brand match and price gap of clicked vs shown products.

    Category is fixed by the query, so brand and price are the personalization signal inside one result list.
    price_gap is |log(price / intent price target)|; lower means a better fit.
    """
    clicked = defaultdict(set)
    for e in events:
        if e["event_type"] == "click":
            clicked[e["search_id"]].add(e["product_id"])
    sums = defaultdict(float)
    searches = 0
    for e in events:
        if e["event_type"] != "search" or e["intent_category"] is None or not clicked[e["search_id"]]:
            continue
        searches += 1
        for product_id, _, _, brand, price in judged[e["query_id"]]:
            groups = ["shown"] + (["clicked"] if product_id in clicked[e["search_id"]] else [])
            gap = abs(math.log(max(price, 0.01) / e["intent_price_target"]))
            for g in groups:
                sums[f"n_{g}"] += 1
                sums[f"gap_{g}"] += gap
                if e["intent_brand"]:
                    sums[f"nb_{g}"] += 1
                    sums[f"brand_{g}"] += brand == e["intent_brand"]
    ratio = lambda a, b: round(sums[a] / sums[b], 4) if sums[b] else 0.0
    return {"searches": searches,
            "brand_match_shown": ratio("brand_shown", "nb_shown"),
            "brand_match_clicked": ratio("brand_clicked", "nb_clicked"),
            "price_gap_shown": ratio("gap_shown", "n_shown"),
            "price_gap_clicked": ratio("gap_clicked", "n_clicked")}


def intent_lift_problems(lift: dict) -> list[str]:
    """Why the planted intent is too weak for L2 to learn from; empty if it's fine."""
    problems = []
    if lift["brand_match_shown"] < 0.2:
        problems.append(f"intent brand is rarely shown ({lift['brand_match_shown']:.1%} of shown products)")
    if lift["brand_match_clicked"] < lift["brand_match_shown"] + 0.1:
        problems.append("clicks barely favour the intent brand")
    if lift["price_gap_clicked"] > 0.8 * lift["price_gap_shown"]:
        problems.append("clicks barely favour the intent price")
    return problems
