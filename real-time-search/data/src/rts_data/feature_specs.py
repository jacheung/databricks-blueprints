"""Clickstream feature catalog: one spec per Feature View, generated from config.

Plain data so it can be tested without the Feature Engineering client; the feature_views notebook turns each spec
into a `Feature` over `session_events`. Groups: `session` (in-session intent, also guests), `user` (long-term, logged-in
only), `item` (product popularity).

Category affinity is one feature per group keyed on (entity, category). Each L2 candidate row carries its own
`category`, so the lookup returns this session's (or user's) count for that candidate's category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta


ENGAGED = "event_type IN ('click', 'add_to_cart')"
ENTITY = {"session": "session_id", "user": "user_id", "item": "product_id"}


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str                    # session | user | item
    op: str                       # Count | Avg | Min | Max | StddevPop | ApproxPercentile | ColumnSelection
    input: str
    filter: str | None            # SQL WHERE applied to session_events before aggregating
    window: timedelta | None      # None for ColumnSelection (latest value)
    params: dict = field(default_factory=dict)
    extra_keys: tuple = ()        # lookup keys after the group's entity, e.g. ("category",)
    prefix: str = ""              # "value." when the source is a Kafka StreamSource

    @property
    def entity(self) -> list[str]:
        return [self.prefix + k for k in (ENTITY[self.group], *self.extra_keys)]

    @property
    def timeseries(self) -> str:
        return f"{self.prefix}event_time"


def feature_specs(cfg: dict, session_prefix: str = "") -> list[FeatureSpec]:
    """`session_prefix="value."` builds session features for a Kafka StreamSource; user/item stay on Delta."""
    fs = cfg["feature_store"]
    sess_w = timedelta(minutes=fs["session_window_minutes"])
    user_w, item_w = timedelta(days=fs["user_window_days"]), timedelta(days=fs["item_window_days"])
    s_sfx, u_sfx, i_sfx = f"{fs['session_window_minutes']}m", f"{fs['user_window_days']}d", f"{fs['item_window_days']}d"
    user_engaged = f"user_id IS NOT NULL AND {ENGAGED}"

    p = session_prefix
    engaged = f"{p}event_type IN ('click', 'add_to_cart')"

    def session(name, op, column, where, window, **kw):
        return FeatureSpec(name, "session", op, p + column, where, window, prefix=p, **kw)

    specs = [
        # Engagement depth.
        session(f"sess_n_clicks_{s_sfx}", "Count", "product_id", f"{p}event_type = 'click'", sess_w),
        session(f"sess_n_carts_{s_sfx}", "Count", "product_id", f"{p}event_type = 'add_to_cart'", sess_w),
        session(f"sess_n_searches_{s_sfx}", "Count", "search_id", f"{p}event_type = 'search'", sess_w),
        # Price tier state (streaming has no median; mean and spread stand in).
        session(f"sess_avg_price_{s_sfx}", "Avg", "price", engaged, sess_w),
        session(f"sess_min_price_{s_sfx}", "Min", "price", engaged, sess_w),
        session(f"sess_max_price_{s_sfx}", "Max", "price", engaged, sess_w),
        session(f"sess_std_price_{s_sfx}", "StddevPop", "price", engaged, sess_w),
        # Category affinity: engaged events in the candidate's category.
        session(f"sess_cat_cnt_{s_sfx}", "Count", "product_id", engaged, sess_w, extra_keys=("category",)),
    ]
    # Most recent micro-intent.
    specs += [session(f"sess_last_{col}", "ColumnSelection", col, engaged, None) for col in ("category", "brand", "price")]
    specs.append(session("sess_last_event_type", "ColumnSelection", "event_type", None, None))

    specs += [
        FeatureSpec(f"user_median_price_{u_sfx}", "user", "ApproxPercentile", "price", user_engaged, user_w,
                    {"percentile": 0.5}),
        FeatureSpec(f"user_n_clicks_{u_sfx}", "user", "Count", "product_id",
                    "user_id IS NOT NULL AND event_type = 'click'", user_w),
        FeatureSpec(f"user_n_carts_{u_sfx}", "user", "Count", "product_id",
                    "user_id IS NOT NULL AND event_type = 'add_to_cart'", user_w),
    ]
    specs.append(FeatureSpec(f"user_cat_cnt_{u_sfx}", "user", "Count", "product_id", user_engaged, user_w,
                             extra_keys=("category",)))

    specs += [
        FeatureSpec(f"item_clicks_{i_sfx}", "item", "Count", "session_id", "event_type = 'click'", item_w),
        FeatureSpec(f"item_carts_{i_sfx}", "item", "Count", "session_id", "event_type = 'add_to_cart'", item_w),
    ]
    return specs
