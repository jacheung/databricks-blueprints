"""Builds docs/ml_pipeline.svg: L2 training and serving, from the data pipeline's tables to the endpoints the app calls.

Run from real-time-search/: uv run --no-project --with fonttools --with brotli python docs/make_ml_pipeline.py

Same conventions as make_data_pipeline.py (brand palette, DM Sans titles subset and inlined, icons as data URIs,
moving dashed flows): GitHub renders README SVGs through <img>, which won't load external files or run script.
Fails if a title or subheading is wider than its box.
"""

import base64
import io
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent
NAVY, GRAY, LAVA, LINE = "#1B3139", "#5A6F77", "#FF3621", "#8A9BA3"
BODY_FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
TITLE, SUB = 16, 12  # titles (DM Sans) and subheadings (system font), as in make_architecture.py
ICON, LINE_GAP = 24, 17
SUB_CHAR = 6.4  # generous average glyph width of the 12 px system font, for the overflow check

ICONS = {  # name -> (file, width / height)
    "apps": ("apps-icon-full-color.svg", 1.0),
    "lakebase": ("lakebase-icon-full-color.svg", 1.0),
    "training": ("primary-icon-orange-model-training.svg", 1.0),
    "ai_runtime": ("ai-runtime-icon-full-color.svg", 1.0),
    "mlflow": ("mlflow-logo-black-rgb.svg", 954 / 408),
    "serving": ("primary-icon-orange-productionize.svg", 1.0),
    "delta": ("primary-icon-orange-delta-table.svg", 1.0),
    "feature_store": ("primary-icon-orange-feature-store.svg", 1.0),
}

FLOWS = {  # brand palette, as in make_architecture.py: Blue 500, Yellow 600, Navy 500, Lava 500
    "qac": ("#4299E0", "QAC"),
    "serp": ("#FFAB00", "SERP"),
    "offline": ("#618794", "offline (tables, training sets)"),
    "fresh": ("#FF3621", "online features"),
}

font = instancer.instantiateVariableFont(TTFont(HERE / "fonts" / "DMSans-VariableFont.ttf"),
                                         {"wght": 600, "opsz": 14})
cmap, hmtx, upm = font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm
title_chars = set()
out = []


def title_width(s, size=TITLE):
    return sum(hmtx[cmap[ord(c)]][0] for c in s) * size / upm


def img(name, x, y, h):
    file, aspect = ICONS[name]
    data = base64.b64encode((HERE / "icons" / file).read_bytes()).decode()
    mime = "image/png" if file.endswith(".png") else "image/svg+xml"
    out.append(f'<image x="{x:.1f}" y="{y:.1f}" width="{h * aspect:.1f}" height="{h:.1f}" '
               f'href="data:{mime};base64,{data}"/>')
    return h * aspect


def box(x, y, w, h, fill="#FFFFFF", stroke=LINE, sw=1, rx=8, dash=False):
    d = ' stroke-dasharray="5 4"' if dash else ""
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
               f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')


def title(x, y, s, color=NAVY, anchor="middle", size=None):
    title_chars.update(s)
    style = f' style="font-size: {size}px"' if size else ""
    out.append(f'<text x="{x:.1f}" y="{y}" class="t"{style} fill="{color}" text-anchor="{anchor}">{escape(s)}</text>')


def sub(x, y, s, color=GRAY, anchor="middle", weight=400):
    out.append(f'<text x="{x}" y="{y}" font-size="{SUB}" font-weight="{weight}" fill="{color}" '
               f'text-anchor="{anchor}">{escape(s)}</text>')


def metric(s):
    """A subheading line drawn bold in navy: the evaluation metrics."""
    return ("metric", s)


def node(icon, name, x, y, w, *lines, dashed=False):
    """A box: icon (optional) + name, or the MLflow wordmark when name is None, then subheading lines.
    Returns (top, bottom, middle y, left, right)."""
    h = 42 + LINE_GAP * len(lines) + 6
    box(x, y, w, h, dash=dashed)
    cx = x + w / 2
    if name is None:  # the logo is the product name (MLflow wordmark), as in make_architecture.py
        img(icon, cx - ICONS[icon][1] * 40 / 2, y + 2, 40)
    else:
        iw = ICONS[icon][1] * ICON if icon else 0
        gap = 8 if icon else 0
        need = iw + gap + title_width(name)
        assert need <= w - 16, f"title {name!r} needs {need:.0f} px in a {w} px box"
        left = cx - need / 2
        if icon:
            img(icon, left, y + 11, ICON)
        title(left + iw + gap, y + 29, name, anchor="start")
    for i, line in enumerate(lines):
        bold = isinstance(line, tuple)
        text = line[1] if bold else line
        assert len(text) * SUB_CHAR <= w - 12, f"subheading {text!r} is too wide for a {w} px box"
        sub(cx, y + 52 + LINE_GAP * i, text, NAVY if bold else GRAY, weight=600 if bold else 400)
    return y, y + h, y + h / 2, x, x + w


def arrow(d, flow, head=True, width=2.5):
    end = f' marker-end="url(#{flow})"' if head else ""
    out.append(f'<path d="{d}" class="flow" fill="none" stroke="{FLOWS[flow][0]}" stroke-width="{width}"{end}/>')


def label(x, y, s, anchor="middle", color=NAVY, weight=500):
    sub(x, y, s, color, anchor, weight)


def panel(x, y, w, h, name, subtitle, tint="#FFFFFF", stroke="#C9D3D8"):
    box(x, y, w, h, fill=tint, stroke=stroke, rx=14)
    title(x + 20, y + 30, name, LAVA, "start")
    if subtitle:
        sub(x + 20, y + 49, subtitle, anchor="start")


def at(mid, h):
    """Top y of a box of height h centered on mid."""
    return mid - h / 2


def height(n):
    return 42 + LINE_GAP * n + 6


W, H = 1400, 830
HEAD = 4  # arrowheads end this far before a box edge
TOP, BOTTOM = 96, 750  # the panels
QAC, SERP, FEAT = 240, 445, 640  # row centers
# panel x, width; nodes sit 15 px inside; the buses run in the gaps between panels
P1, P2, P3, P4, P5 = (20, 240), (300, 270), (610, 270), (920, 240), (1200, 180)
BUS1, BUS2, BUS3, BUS4 = 280, 590, 900, 1180


def col(p):
    return p[0] + 15, p[1] - 30


# --- Header --------------------------------------------------------------------------------------------------------
title(30, 38, "ML pipeline: features → the rerankers the app calls", anchor="start", size=20)
sub(30, 62, "L2 trains on what L1 retrieves, with features as of each search, is measured against L1, and is served "
            "with the same features online.", anchor="start")

# --- 1. Inputs ----------------------------------------------------------------------------------------------------
panel(P1[0], TOP, P1[1], BOTTOM - TOP, "INPUTS", "from the data pipeline and L1", "#FFF6F3", "#FFC9BE")
x, w = col(P1)
logs = node("delta", "Search logs", x, at(QAC, height(2)), w, "qac / serp_impressions",
            "submitted · clicked · carted")
cands = node("delta", "L1 candidates", x, at(SERP, height(2)), w, "QAC: prefix lookup, top 20",
             "SERP: AI Search, top 200")
views = node("feature_store", "Feature Views", x, at(FEAT, height(2)), w, "session · user · item",
             "offline tables, full history")

# --- 2. Training sets ----------------------------------------------------------------------------------------------
panel(P2[0], TOP, P2[1], BOTTOM - TOP, "TRAINING SETS", "train on what L1 returns")
x, w = col(P2)
qac_set = node("delta", "QAC training set", x, at(QAC, height(3)), w, "search × prefix × L1 top 20",
               "label: the query submitted", "+ features as of the search")
serp_set = node("delta", "SERP training set", x, at(SERP, height(3)), w, "search × L1 top 200",
                "label: cart 2 · click 1 · else 0", "+ features as of the search")
sub(x + w / 2, FEAT - 8, "create_training_set joins the", NAVY, weight=500)
sub(x + w / 2, FEAT + 9, "features point-in-time: no leakage")

# every input feeds both training sets: a bus in the gap
arrow(f"M{logs[4]} {logs[2]} H{x - HEAD}", "offline")
arrow(f"M{cands[4]} {cands[2]} H{x - HEAD}", "offline")
arrow(f"M{views[4]} {views[2]} H{BUS1} V{logs[2]}", "offline", head=False)
arrow(f"M{BUS1} {logs[2]} H{x - HEAD}", "offline", head=False)

# --- 3. Train + evaluate -------------------------------------------------------------------------------------------
panel(P3[0], TOP, P3[1], BOTTOM - TOP, "TRAIN + EVALUATE", "L1 → L2 on held-out searches")
x, w = col(P3)
qac_model = node("training", "QAC reranker", x, at(QAC, height(4)), w, "LightGBM LambdaRank",
                 "Serverless CPU", metric("MRR@5  0.539 → 0.598"), metric("success@1  0.458 → 0.521"))
serp_model = node("ai_runtime", "SERP reranker", x, at(SERP, height(3)), w, "DCNv2 (deep & cross network)",
                  "AI Runtime · one A10 GPU", metric("NDCG@10  0.224 → 0.289"))
node(None, "Read the lift per slice", x, at(FEAT, height(4)), w, "paired bootstrap 95% CIs",
     "first vs 2nd+ search · guest", "shopper intent: none / category", "L2 can lose to L1 on some slices",
     dashed=True)

arrow(f"M{qac_set[4]} {QAC} H{x - HEAD}", "qac")
arrow(f"M{serp_set[4]} {SERP} H{x - HEAD}", "serp")

# --- 4. Register + serve -------------------------------------------------------------------------------------------
panel(P4[0], TOP, P4[1], BOTTOM - TOP, "REGISTER + SERVE", "Unity Catalog · Model Serving", "#F4F7F9")
x, w = col(P4)
cx = x + w / 2
registry = node("mlflow", None, x, at(QAC, height(3)), w, "Unity Catalog registry",
                "fe.log_model: the model", "carries its feature lookups")
serving = node("serving", "Model Serving", x, at(SERP, height(3)), w, "QAC L2 · CPU · p95 ~35 ms",
               "SERP L2 · T4 GPU", "route-optimized")
lakebase = node("lakebase", "Lakebase", x, at(FEAT, height(2)), w, "online features",
                "session < 1 s · user · item")

arrow(f"M{qac_model[4]} {QAC - 8} H{x - HEAD}", "qac")
arrow(f"M{serp_model[4]} {SERP} H{BUS3} V{QAC + 8} H{x - HEAD}", "serp")
arrow(f"M{cx - 10} {registry[1]} V{serving[0] - HEAD}", "qac")
arrow(f"M{cx + 10} {registry[1]} V{serving[0] - HEAD}", "serp")
label(cx + 20, (registry[1] + serving[0]) / 2 + 4, "latest version", anchor="start")
arrow(f"M{cx} {lakebase[0]} V{serving[1] + HEAD}", "fresh")
label(cx + 10, (serving[1] + lakebase[0]) / 2 + 4, "looks up features by key", anchor="start")

# --- 5. Search app -------------------------------------------------------------------------------------------------
panel(P5[0], TOP, P5[1], BOTTOM - TOP, "SEARCH APP", "Databricks Apps")
x, w = col(P5)
app = node("apps", "Search app", x, at(SERP, height(2)), w, "sends keys + the", "L1 candidates")
arrow(f"M{serving[4]} {SERP - 8} H{x - HEAD}", "qac")
arrow(f"M{serving[4]} {SERP + 8} H{x - HEAD}", "serp")
sub(x + w / 2, app[0] - 14, "reranked results")
for i, line in enumerate(["falls back to the L1", "order when L2 is slow", "(36 ms QAC, 200 ms SERP)"]):
    sub(x + w / 2, app[1] + 24 + LINE_GAP * i, line)

# the same Feature Views, materialized online: training and serving read one definition
LOOP_Y = BOTTOM + 22
arrow(f"M{views[3] + 60} {views[1]} V{LOOP_Y} H{cx} V{lakebase[1] + HEAD}", "fresh")
label((P2[0] + P4[0]) / 2 + 60, LOOP_Y + 18, "the same Feature Views, kept fresh online: training and serving "
                                             "can't drift")

# --- Legend (top right, stacked as in make_architecture.py) -------------------------------------------------------
LEGEND_X = W - 20 - 48 - max(title_width(text, 14) for _, text in FLOWS.values())
for i, (flow, (color, text)) in enumerate(FLOWS.items()):
    y = 22 + 19 * i
    out.append(f'<path d="M{LEGEND_X:.0f} {y - 4.5} H{LEGEND_X + 38:.0f}" fill="none" stroke="{color}" '
               f'stroke-width="3.5" stroke-dasharray="8 6"/>')
    title(LEGEND_X + 50, y, text, anchor="start", size=14)

# --- Font + write --------------------------------------------------------------------------------------------------
opts = subset.Options()
opts.flavor = "woff2"
subsetter = subset.Subsetter(opts)
subsetter.populate(text="".join(sorted(title_chars)))
subsetter.subset(font)
buf = io.BytesIO()
font.flavor = "woff2"
font.save(buf)
font_b64 = base64.b64encode(buf.getvalue()).decode()

markers = "\n".join(
    f'<marker id="{flow}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4.5" markerHeight="4.5" orient="auto">'
    f'<path d="M0 0 L10 5 L0 10 z" fill="{color}"/></marker>' for flow, (color, _) in FLOWS.items())

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{BODY_FONT}">
<defs>
<style>
@font-face {{ font-family: "DM Sans"; font-weight: 600; src: url(data:font/woff2;base64,{font_b64}) format("woff2"); }}
.t {{ font-family: "DM Sans", {BODY_FONT}; font-size: {TITLE}px; font-weight: 600; }}
.flow {{ stroke-dasharray: 8 6; animation: flow 0.8s linear infinite; }}
@keyframes flow {{ to {{ stroke-dashoffset: -14; }} }}
@media (prefers-reduced-motion: reduce) {{ .flow {{ animation: none; }} }}
</style>
{markers}
</defs>
<rect width="{W}" height="{H}" fill="#FFFFFF"/>
{chr(10).join(out)}
</svg>
"""
(HERE / "ml_pipeline.svg").write_text(svg)
print("wrote", HERE / "ml_pipeline.svg", f"({len(svg) // 1024} KB)")
