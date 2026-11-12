"""Builds docs/flow.svg: wide source table -> conditional MERGE split -> narrow CDF tables -> one index each.

Uses the diagramsmith skill (agent-config). Run from split-source-table-per-index/:
  uv run --no-project --with fonttools --with brotli python docs/make_flow.py [--theme databricks] [--png]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "diagramsmith" / "scripts"))
from diagramsmith import HEAD, Diagram, args  # noqa: E402

DELTA, JOB, APPS = "primary-icon-orange-delta-table.svg", "primary-icon-orange-productionize.svg", \
    "apps-icon-full-color.svg"

opt = args(Path(__file__).parent / "flow.svg")
TOP, BOTTOM = 96, 530
d = Diagram(1400, 590, opt.theme, flows={
    "write": "Delta writes",
    "cdf": "change feed → index sync",
    "query": "query time",
})
# panel x, width; nodes sit 15 px inside; the buses run in the gaps between panels
P1, P2, P3, P4, P5 = (20, 260), (320, 270), (630, 290), (960, 230), (1230, 150)
BUS2, BUS4 = 610, 1210


def col(p):
    return p[0] + 15, p[1] - 30


d.header("One wide table → one narrow table per index → one index each",
         "Each narrow table's change feed only records edits to its own columns, so each index only syncs "
         "the changes it uses.")
d.legend()

# --- 1. Source -----------------------------------------------------------------------------------------------------
d.panel(P1[0], TOP, P1[1], BOTTOM - TOP, "1 · SOURCE", "wide table, embedded once", tint="warm")
x, w = col(P1)
writes = d.node(None, "Upstream writes", x, 160, w, "inserts · updates · deletes")
embed = d.node(None, "Embed body once", x, 270, w, "ai_query(gte-large-en, body)", "re-embed only if body changed")
wide = d.node(DELTA, "docs_wide", x, 400, w, "id · title (A) · body (B)", "category (C) · body_vector")
d.varrow(x + w / 2, writes[1], embed[0], "write")
d.varrow(x + w / 2, embed[1], wide[0], "write")

# --- 2. Split ------------------------------------------------------------------------------------------------------
d.panel(P2[0], TOP, P2[1], BOTTOM - TOP, "2 · SPLIT", "Lakeflow Job, before each sync")
x, w = col(P2)
merge = d.node(JOB, "Conditional MERGE", x, 330, w, "one per narrow table", "UPDATE only if its cols changed",
               "INSERT new · DELETE removed", accent=True)
d.arrow(f"M{wide[4]} {wide[2]} H{(wide[4] + x) / 2} V{merge[2]} H{x - HEAD}", "write")
d.sub(x + w / 2, merge[1] + 30, "NOT (t.col <=> s.col) for each col")
d.sub(x + w / 2, merge[1] + 47, "a no-op update writes no CDF row")

# --- 3. Narrow tables ----------------------------------------------------------------------------------------------
d.panel(P3[0], TOP, P3[1], BOTTOM - TOP, "3 · NARROW TABLES", "delta.enableChangeDataFeed = true")
x, w = col(P3)
nar_a = d.node(DELTA, "docs_idx_title", x, 190, w, "id · title · body · body_vector", "CDF: title / body edits")
nar_b = d.node(DELTA, "docs_idx_category", x, 410, w, "id · body · category · body_vector",
               "CDF: body / category edits")
d.arrow(f"M{merge[4]} {merge[2] - 8} H{BUS2 - 6} V{nar_a[2]} H{x - HEAD}", "write")
d.arrow(f"M{merge[4]} {merge[2] + 8} H{BUS2 + 6} V{nar_b[2]} H{x - HEAD}", "write")

# --- 4. Indexes ----------------------------------------------------------------------------------------------------
d.panel(P4[0], TOP, P4[1], BOTTOM - TOP, "4 · AI SEARCH", "Delta Sync · self-managed", tint="cool")
x, w = col(P4)
idx_a = d.node(None, "Index A", x, 190, w, "embedding_vector_column", "TRIGGERED sync")
idx_b = d.node(None, "Index B", x, 410, w, "embedding_vector_column", "TRIGGERED sync")
d.arrow(f"M{nar_a[4]} {nar_a[2]} H{x - HEAD}", "cdf")
d.arrow(f"M{nar_b[4]} {nar_b[2]} H{x - HEAD}", "cdf")

# --- 5. Query ------------------------------------------------------------------------------------------------------
d.panel(P5[0], TOP, P5[1], BOTTOM - TOP, "5 · QUERY", "your app")
x, w = col(P5)
app = d.node(APPS, "App", x, 300, w, "embed query", "same model", "→ query_vector")
d.arrow(f"M{x} {app[2] - 8} H{BUS4 + 4} V{idx_a[2]} H{idx_a[4] + HEAD}", "query")
d.arrow(f"M{x} {app[2] + 8} H{BUS4 - 4} V{idx_b[2]} H{idx_b[4] + HEAD}", "query")

d.sub(30, BOTTOM + 30, "Example: an edit to category only reaches docs_idx_category's change feed. "
                       "docs_idx_title's MERGE condition is false for that row, so Index A's next sync has nothing "
                       "to process.", anchor="start")

d.save(opt.out, png=opt.png)
