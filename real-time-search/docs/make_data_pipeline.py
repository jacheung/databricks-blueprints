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

FLOWS = {  # brand palette: Navy 500, Lava 500, Blue 500, Yellow 600
    "batch": ("#618794", "batch (Delta tables)"),
    "stream": ("#FF3621", "streaming (Kafka, Real-Time Mode)"),
    "online": ("#4299E0", "into the stores the app reads"),
    "ml": ("#FFAB00", "L1 retrieval stores (ml track)"),
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


W, H = 1400, 880
HEAD = 4  # arrowheads end this far before a box edge
TOP, BOTTOM = 96, 626  # the top row of panels
LOW = 656  # the bottom row of panels

# --- Header --------------------------------------------------------------------------------------------------------
title(30, 38, "Data pipeline: pinned source data → the stores the app reads", anchor="start", size=20)
sub(30, 62, "data bundle jobs, in order: catalog_job → clickstream_job → demo_job (before each demo). "
            "The ml track builds the L1 retrieval stores from the same tables.", anchor="start")

# --- 1. Source data ------------------------------------------------------------------------------------------------
panel(30, TOP, 240, BOTTOM - TOP, "SOURCE DATA", "pinned in the repo", "#FFF6F3", "#FFC9BE")
esci = node(None, "Amazon ESCI", 50, 166, 200, "queries · products · labels", "esci-data @ 1ebba06 · sha256")
snap = node(None, "Category snapshot", 50, 306, 200, "ai_classify answers", "data/snapshots · 149 KB")
seed = node(None, "Config + seed", 50, 446, 200, "20 categories · prices", "simulator seed 42")
sub(150, 566, "same bytes on every rebuild;")
sub(150, 583, "ai_classify only labels rows")
sub(150, 600, "missing from the snapshot")

# --- 2. Catalog ----------------------------------------------------------------------------------------------------
panel(320, TOP, 240, BOTTOM - TOP, "CATALOG", "catalog_job · catalog_prep")
labels = node("delta", "esci_labels", 340, 166, 200, "(query, product) judgements", "E / S / C / I + gain")
queries = node("delta", "queries", 340, 306, 200, "10K queries", "+ category")
products = node("delta", "products", 340, 446, 200, "170K judged products", "+ category · synthetic price")

BUS = 293  # sources -> catalog
arrow(f"M250 {esci[2]} H{340 - HEAD}", "batch")
arrow(f"M{BUS} {esci[2]} V{queries[2]} H{340 - HEAD}", "batch")
arrow(f"M{BUS} {queries[2]} V{products[2]} H{340 - HEAD}", "batch")
arrow(f"M250 {snap[2]} H{BUS}", "batch", head=False)
arrow(f"M250 {seed[2]} H{BUS}", "batch", head=False)

# --- 3. Clickstream + features -------------------------------------------------------------------------------------
CX, CW = 610, 425  # panel
LX, LW = 625, 190  # left column: the simulator's tables
RX, RW = 845, 175  # right column: Kafka and the Feature Views
panel(CX, TOP, CW, BOTTOM - TOP, "CLICKSTREAM + FEATURES", "clickstream_job")
events = node("delta", "session_events", LX, 166, LW, "~1M simulated events", "45 days · CDF on")
sessions = node("delta", "sim_sessions", LX, 286, LW, "planted intent per session")
qac_imp = node("delta", "qac_impressions", LX, 391, LW, "one row per search")
serp_imp = node("delta", "serp_impressions", LX, 496, LW, "judged products shown")
kafka = node("kafka", "Apache Kafka", RX, 166, RW, "events topic")
ingest = node("delta", "events_ingest", RX, 261, RW, "topic copy for training")
rtm = node("rtm", "Real-Time Mode", RX, 356, RW, "12 session features", "< 1 s to Lakebase", accent=True)
fstore = node("feature_store", "Feature Store", RX, 466, RW, "user 30 d · item 7 d", "hourly batch windows")

SIM_BUS = 585  # catalog -> clickstream_sim
arrow(f"M540 {queries[2]} H{SIM_BUS} V{events[0] + 18} H{LX - HEAD}", "batch")
arrow(f"M540 {labels[2]} H{SIM_BUS}", "batch", head=False)
arrow(f"M540 {products[2]} H{SIM_BUS} V{queries[2]}", "batch", head=False)
label(SIM_BUS, 158, "clickstream_sim")
DERIVED = 617  # session_events -> the other simulator tables
arrow(f"M{LX + 30} {events[1]} V{events[1] + 14} H{DERIVED} V{sessions[2]} H{LX - HEAD}", "batch")
arrow(f"M{DERIVED} {sessions[2]} V{qac_imp[2]} H{LX - HEAD}", "batch")
arrow(f"M{DERIVED} {qac_imp[2]} V{serp_imp[2]} H{LX - HEAD}", "batch")
# events -> Kafka (replayed with their event times) -> ingestion copy and Real-Time Mode
arrow(f"M{LX + LW} {kafka[2]} H{RX - HEAD}", "stream")
label((LX + LW + RX) / 2, 158, "kafka_stream")
arrow(f"M{RX + 50} {kafka[1]} V{ingest[0] - HEAD}", "stream")
# around the ingestion copy, down the panel's right edge, into Real-Time Mode from above
arrow(f"M{RX + RW} {kafka[2] - 8} H{RX + RW + 8} V{rtm[0] - 14} H{RX + RW - 30} V{rtm[0] - HEAD}", "stream")
# Delta events -> the batch user / item features
arrow(f"M{LX + LW} {events[1] - 18} H{(LX + LW + RX) / 2} V{fstore[2]} H{RX - HEAD}", "batch")
label(RX + RW - 4, BOTTOM - 14, "feature_views", anchor="end")

# --- 4. What the app reads -----------------------------------------------------------------------------------------
AX, AW = 1085, 290
NX, NW = AX + 18, AW - 36
panel(AX, TOP, AW, BOTTOM - TOP, "WHAT THE APP READS", "online stores, read per request", "#F4F7F9")
online = node("lakebase", "Lakebase", NX, 166, NW, "online features: session,", "user and item (L2 lookups)",
              accent=True)
demo_store = node("lakebase", "Lakebase", NX, 276, NW, "demo scenarios + personas", "synced tables")
prefix_store = node("lakebase", "Lakebase", NX, 386, NW, "QAC prefix completions", "synced table · QAC L1")
index = node("lakebase", "AI Search", NX, 496, NW, "products_hybrid_idx", "hybrid search · SERP L1")

FEAT_BUS, DEMO_BUS, ML_BUS = 1048, 1060, 1072  # the gap between panels 3 and 4
arrow(f"M{RX + RW} {rtm[2]} H{FEAT_BUS} V{online[2]} H{NX - HEAD}", "online")
arrow(f"M{RX + RW} {fstore[2]} H{FEAT_BUS} V{rtm[2]}", "online", head=False)

# --- 5. L1 retrieval (ml track), below the catalog ------------------------------------------------------------------
panel(320, LOW, 240, H - 20 - LOW, "L1 RETRIEVAL", "ml track · retrieval_job")
prefix = node(None, "qac_prefix_completions", 335, 722, 210, "popularity · CTR per prefix", dashed=True)
search = node(None, "products_search", 335, 797, 210, "index source · CDF on", dashed=True)
arrow(f"M{products[3] + 175} {products[1]} V{prefix[0] - HEAD}", "ml")  # products -> both (clear of the heading)
arrow(f"M{products[3] + 12} {products[1]} V{products[1] + 20} H{328} V{search[2]} H{335 - HEAD}", "ml")
IMP_X = 575  # impressions -> prefix completions
arrow(f"M{LX + 80} {serp_imp[1]} V{BOTTOM + 8} H{IMP_X} V{prefix[0] + 18} H{545 + HEAD + 2}", "ml")
OUT_Y = BOTTOM + 16  # L1 stores -> the app's stores, above the bottom panels
arrow(f"M545 {prefix[2] + 10} H{597} V{OUT_Y} H{ML_BUS} V{prefix_store[2]} H{NX - HEAD}", "ml")
arrow(f"M{ML_BUS} {prefix_store[2]} V{index[2]} H{NX - HEAD}", "ml")
arrow(f"M545 {search[2]} H{597} V{prefix[2] + 10}", "ml", head=False)

# --- 6. Demo, below the clickstream --------------------------------------------------------------------------------
panel(CX, LOW, CW, H - 20 - LOW, "DEMO", "demo_job · before each demo")
shift = node(None, "time_shift", LX + 10, 730, 150, "events end yesterday", "+ reruns user / item")
personas = node("delta", "demo_personas", RX - 35, 722, RW + 35, "from user features")
scenarios = node("delta", "demo_scenarios", RX - 35, 797, RW + 35, "replayable sessions")
SHIFT_X = 600  # time_shift moves session_events forward (in place)
arrow(f"M{LX + 10} {shift[2]} H{SHIFT_X} V{events[0] + 52} H{LX - HEAD}", "batch")
arrow(f"M{fstore[3] + 60} {fstore[1]} V{personas[0] - HEAD}", "batch")  # latest user features -> personas
arrow(f"M{DERIVED} {serp_imp[2]} V{scenarios[2]} H{RX - 35 - HEAD}", "batch")  # sessions + events -> scenarios
arrow(f"M{RX + RW} {scenarios[2]} H{DEMO_BUS} V{demo_store[2]} H{NX - HEAD}", "online")
arrow(f"M{RX + RW} {personas[2]} H{DEMO_BUS}", "online", head=False)

# --- 7. App + legend -----------------------------------------------------------------------------------------------
app = node("apps", "Apps", AX + 18, 716, AW - 36, "rts-search · QAC + SERP", "replays scenarios as Play")
arrow(f"M{AX + AW / 2} {BOTTOM} V{app[0] - HEAD}", "online")
label(AX + AW / 2 - 8, LOW + 40, "reads per keystroke / search", anchor="end")
# the app's own events go to the same topic (live clicks, replayed scenarios)
arrow(f"M{AX + AW - 18} {app[2]} H{W - 10} V80 H{RX + RW - 20} V{kafka[0] - HEAD}", "stream")
label(W - 16, 92, "live events · Play", anchor="end")

box(30, LOW, 240, H - 20 - LOW, rx=14, stroke="#C9D3D8")
title(50, LOW + 30, "LEGEND", LAVA, "start")
for i, (flow, (color, text)) in enumerate(FLOWS.items()):
    y = LOW + 62 + 26 * i
    out.append(f'<path d="M50 {y - 4} H84" fill="none" stroke="{color}" stroke-width="3.5" stroke-dasharray="8 6"/>')
    sub(94, y, text, NAVY, "start")
box(50, LOW + 160, 34, 22, rx=5, stroke=LINE, dash=True)
sub(94, LOW + 175, "owned by the ml track", NAVY, "start")

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
