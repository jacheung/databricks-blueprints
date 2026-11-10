"""Builds docs/data_pipeline.svg: the data engineering pipeline, from the pinned source data to what the app reads.

Run from real-time-search/: uv run --no-project --with fonttools --with brotli python docs/make_data_pipeline.py

Same conventions as make_architecture.py (brand palette, DM Sans titles subset and inlined, icons as data URIs,
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
    "rtm": ("primary-icon-orange-accelerator-aware-scheduler.svg", 1.0),
    "kafka": ("apache-kafka-logo.png", 500 / 812),
    "delta": ("primary-icon-orange-delta-table.svg", 1.0),
    "feature_store": ("primary-icon-orange-feature-store.svg", 1.0),
}

FLOWS = {  # brand palette: Navy 500, Lava 500, Blue 500
    "batch": ("#618794", "batch (Delta tables)"),
    "stream": ("#FF3621", "streaming (Kafka, Real-Time Mode)"),
    "online": ("#4299E0", "into the stores the app reads"),
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


def node(icon, name, x, y, w, *lines, accent=False, dashed=False):
    """A box: icon (optional) + name, then subheading lines. Returns (top, bottom, middle y, left, right)."""
    h = 42 + LINE_GAP * len(lines) + 6
    box(x, y, w, h, stroke=LAVA if accent else LINE, sw=2 if accent else 1, dash=dashed)
    cx = x + w / 2
    iw = ICONS[icon][1] * ICON if icon else 0
    gap = 8 if icon else 0
    need = iw + gap + title_width(name)
    assert need <= w - 16, f"title {name!r} needs {need:.0f} px in a {w} px box"
    left = cx - need / 2
    if icon:
        img(icon, left, y + 11, ICON)
    title(left + iw + gap, y + 29, name, anchor="start")
    for i, line in enumerate(lines):
        assert len(line) * SUB_CHAR <= w - 12, f"subheading {line!r} is too wide for a {w} px box"
        sub(cx, y + 52 + LINE_GAP * i, line)
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


W, H = 1400, 840
HEAD = 4  # arrowheads end this far before a box edge
TOP, BOTTOM = 96, 780  # the panels
# panel x, width; nodes sit 15 px inside; the buses run in the gaps between panels
P1, P2, P3, P4, P5 = (20, 240), (300, 250), (585, 270), (890, 250), (1180, 200)
BUS1, BUS2, BUS3, BUS4 = 272, 567, 872, 1160


def col(p):
    return p[0] + 15, p[1] - 30


# --- Header --------------------------------------------------------------------------------------------------------
title(30, 38, "Data pipeline: your search data → the stores the app reads", anchor="start", size=20)
sub(30, 62, "Batch features refresh hourly, session features in under a second, and the app's own events stream "
            "back in.", anchor="start")

# --- 1. Your data --------------------------------------------------------------------------------------------------
panel(P1[0], TOP, P1[1], BOTTOM - TOP, "YOUR DATA", "what you bring", "#FFF6F3", "#FFC9BE")
x, w = col(P1)
catalog = node(None, "Product catalog", x, 161.5, w, "SKUs · titles · attributes", "brands · prices")
logs = node(None, "Search logs", x, 390, w, "queries · impressions", "clicks · relevance labels")
clicks = node(None, "Clickstream", x, 605, w, "searches · clicks · carts", "web + app sessions")
sub(P1[0] + P1[1] / 2, 732, "in this blueprint: Amazon ESCI")
sub(P1[0] + P1[1] / 2, 749, "+ simulated sessions")

# --- 2. Lakehouse --------------------------------------------------------------------------------------------------
panel(P2[0], TOP, P2[1], BOTTOM - TOP, "LAKEHOUSE", "Delta tables · Kafka topic")
x, w = col(P2)
classify = node(None, "ai_classify", x, 170, w, "LLM-enriched categories")
products = node("delta", "Products", x, 260, w, "products", "+ category · price")
slogs = node("delta", "Search logs", x, 390, w, "queries · esci_labels", "qac / serp_impressions")
events = node("delta", "Session events", x, 540, w, "session_events", "behavior history")
kafka = node("kafka", "Apache Kafka", x, 670, w, "events topic", "site + app clicks")

arrow(f"M{catalog[4]} {catalog[2]} H{x - HEAD}", "batch")
arrow(f"M{x + w / 2} {classify[1]} V{products[0] - HEAD}", "batch")
arrow(f"M{catalog[4]} {logs[2]} H{x - HEAD}", "batch")
arrow(f"M{catalog[4]} {clicks[2] - 8} H{BUS1 - 6} V{events[2]} H{x - HEAD}", "batch")
arrow(f"M{catalog[4]} {clicks[2] + 8} H{BUS1 + 6} V{kafka[2]} H{x - HEAD}", "stream")

# --- 3. Features + retrieval ---------------------------------------------------------------------------------------
panel(P3[0], TOP, P3[1], BOTTOM - TOP, "FEATURES + RETRIEVAL", "batch + Real-Time Mode")
x, w = col(P3)
search_src = node("delta", "Search index source", x, 260, w, "products_search", "synced to AI Search")
prefix = node("delta", "Prefix completions", x, 390, w, "qac_prefix_completions", "popularity · CTR per prefix")
fstore = node("feature_store", "Feature Store", x, 540, w, "user 30 d · item 7 d · hourly",
              "offline tables for training")
rtm = node("rtm", "Real-Time Mode", x, 670, w, "12 session features", "Kafka → Lakebase in < 1 s")

arrow(f"M{products[4]} {products[2]} H{x - HEAD}", "batch")
arrow(f"M{products[4]} {products[2] + 18} H{BUS2} V{prefix[2]}", "batch", head=False)
arrow(f"M{slogs[4]} {slogs[2]} H{x - HEAD}", "batch")
arrow(f"M{events[4]} {events[2]} H{x - HEAD}", "batch")
arrow(f"M{kafka[4]} {kafka[2]} H{x - HEAD}", "stream")

# --- 4. Online stores ----------------------------------------------------------------------------------------------
panel(P4[0], TOP, P4[1], BOTTOM - TOP, "ONLINE STORES", "read per request", "#F4F7F9")
x, w = col(P4)
index = node("lakebase", "AI Search", x, 260, w, "hybrid product index", "SERP retrieval (L1)")
prefix_store = node("lakebase", "Lakebase", x, 390, w, "prefix completions", "QAC retrieval (L1)")
online = node("lakebase", "Lakebase", x, 605, w, "online features", "session · user · item (L2)")

arrow(f"M{search_src[4]} {search_src[2]} H{x - HEAD}", "online")
arrow(f"M{prefix[4]} {prefix[2]} H{x - HEAD}", "online")
arrow(f"M{fstore[4]} {fstore[2]} H{BUS3 - 8} V{online[2] - 8} H{x - HEAD}", "online")
arrow(f"M{rtm[4]} {rtm[2]} H{BUS3 + 8} V{online[2] + 8} H{x - HEAD}", "stream")

# --- 5. Search app -------------------------------------------------------------------------------------------------
panel(P5[0], TOP, P5[1], BOTTOM - TOP, "SEARCH APP", "Databricks Apps")
x, w = col(P5)
app = node("apps", "Search app", x, 420, w, "QAC as you type", "SERP on search")
arrow(f"M{index[4]} {index[2]} H{BUS4} V{app[2]} H{x - HEAD}", "online")
arrow(f"M{prefix_store[4]} {prefix_store[2]} H{BUS4}", "online", head=False)
arrow(f"M{online[4]} {online[2]} H{BUS4} V{app[2]}", "online", head=False)
sub(x + w / 2, app[1] + 24, "reads per keystroke")
sub(x + w / 2, app[1] + 41, "and per search")

# the app's own events go back into the same topic: the loop that keeps session features live
LOOP_Y = BOTTOM + 22
arrow(f"M{x + w} {app[2]} H{W - 8} V{LOOP_Y} H{kafka[3] + 110} V{kafka[1] + HEAD}",
      "stream")
label(P4[0] + P4[1] / 2, LOOP_Y + 18, "live searches and clicks stream back in")

# --- Legend (top right, stacked as in make_architecture.py) -------------------------------------------------------
LEGEND_X = W - 20 - 48 - max(title_width(text, 14) for _, text in FLOWS.values())
for i, (flow, (color, text)) in enumerate(FLOWS.items()):
    y = 34 + 19 * i
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
(HERE / "data_pipeline.svg").write_text(svg)
print("wrote", HERE / "data_pipeline.svg", f"({len(svg) // 1024} KB)")
