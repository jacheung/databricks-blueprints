"""SERP L1: the product text the hybrid index embeds and keyword-matches, and the recall metric it is judged on."""

from __future__ import annotations

import html
import re


def clean_title(title: str | None) -> str:
    """Titles without HTML tags or entities, whitespace collapsed."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", title or ""))
    return re.sub(r"\s+", " ", text).strip()


def search_text(title: str | None, brand: str | None) -> str:
    """Title, then brand if the title doesn't already name it (whole word).

    One column is both embedded and keyword-matched, so brand appears once and a brand match counts once.
    """
    parts = [clean_title(title)]
    brand = (brand or "").strip()
    if brand and not re.search(rf"\b{re.escape(brand.lower())}\b", parts[0].lower()):
        parts.append(brand)
    return " ".join(p for p in parts if p)


def recall_at_k(retrieved: list[str], relevant: set[str], ks: tuple[int, ...]) -> dict[int, float]:
    """Share of the relevant items found in the top k, for each k."""
    if not relevant:
        raise ValueError("recall needs at least one relevant item")
    return {k: len(relevant.intersection(retrieved[:k])) / len(relevant) for k in ks}
