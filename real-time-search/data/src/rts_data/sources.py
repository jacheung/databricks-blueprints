"""Pinned source data, so a rebuild in any workspace starts from the same inputs.

- ESCI: downloaded from a fixed commit of amazon-science/esci-data and checked against the sha256 in its Git LFS
  pointer (the files haven't changed since that commit, 2022-08-07).
- Categories: `ai_classify` is an LLM call, so labels can differ run to run and as the model behind it changes. Its
  answers are committed under `data/snapshots/` (id, category), and catalog_prep calls the LLM only for rows missing
  from them.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io

ESCI_COMMIT = "1ebba061d0cd0d7c5c4edbc9e509ab097b54f593"
ESCI_FILES = {  # name -> (sha256, size in bytes), from the LFS pointers at ESCI_COMMIT
    "shopping_queries_dataset_examples.parquet":
        ("4a735b693b4a424a6fc67f5be6e4c811495c488bbf66d02a602d308b2744263a", 51_286_808),
    "shopping_queries_dataset_products.parquet":
        ("25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265", 1_108_857_465),
}


def esci_url(name: str) -> str:
    return (f"https://media.githubusercontent.com/media/amazon-science/esci-data/{ESCI_COMMIT}/"
            f"shopping_queries_dataset/{name}")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(16 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_category_snapshot(path: str, key: str, rows) -> None:
    """(id, category or None) rows → gzipped CSV sorted by id; the bytes depend only on the rows (mtime 0)."""
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow([key, "category"])
    writer.writerows(sorted((str(k), c or "") for k, c in rows))
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as f:
        f.write(text.getvalue().encode())


def read_category_snapshot(path: str, key: str, allowed: set[str]) -> list[tuple[str, str | None]]:
    """The snapshot's (id, category) rows; None where the LLM gave no answer. Fails on a label outside `allowed`
    (the config's categories changed, so the snapshot is stale: re-classify with use_category_snapshot=false)."""
    with gzip.open(path, "rt", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != [key, "category"]:
            raise ValueError(f"{path}: header {header}, expected [{key!r}, 'category']")
        rows = [(k, c or None) for k, c in reader]
    unknown = sorted({c for _, c in rows if c and c not in allowed})
    if unknown:
        raise ValueError(f"{path}: categories not in the config: {unknown}")
    return rows
