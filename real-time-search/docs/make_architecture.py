"""Builds docs/architecture.svg from docs/icons/ and docs/fonts/.

Run from real-time-search/: uv run --no-project --with fonttools --with brotli python docs/make_architecture.py

Icons and the title font are inlined as data URIs: GitHub renders README SVGs through <img>, which won't load
external files. Titles are DM Sans (SIL Open Font License), pinned to semibold and subset to the characters used.
"""

import base64
import io
from xml.sax.saxutils import escape
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent
NAVY, GRAY, LAVA, LINE = "#1B3139", "#5A6F77", "#FF3621", "#8A9BA3"
BODY_FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
TITLE, SUB = 16, 12  # the only two font sizes: titles (DM Sans) and subheadings (system font)
ICON = 24  # icon height next to a title
LINE_GAP = 17  # between subheading lines

ICONS = {  # name -> (file, width / height)
    "apps": ("apps-icon-full-color.svg", 1.0),
    "lakebase": ("lakebase-icon-full-color.svg", 1.0),
    "rtm": ("primary-icon-orange-accelerator-aware-scheduler.svg", 1.0),
    "training": ("primary-icon-orange-model-training.svg", 1.0),
    "ai_runtime": ("ai-runtime-icon-full-color.svg", 1.0),
    "mlflow": ("mlflow-logo-black-rgb.svg", 954 / 408),
    "kafka": ("apache-kafka-logo.png", 500 / 812),
    "serving": ("primary-icon-orange-productionize.svg", 1.0),
    "delta": ("primary-icon-orange-delta-table.svg", 1.0),
    "feature_store": ("primary-icon-orange-feature-store.svg", 1.0),
}

font = instancer.instantiateVariableFont(TTFont(HERE / "fonts" / "DMSans-VariableFont.ttf"),
                                         {"wght": 600, "opsz": 14})
cmap, hmtx, upm = font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm
title_chars = set()
out = []


def title_width(s):
    return sum(hmtx[cmap[ord(c)]][0] for c in s) * TITLE / upm


def img(name, x, y, h):
    file, aspect = ICONS[name]
    data = base64.b64encode((HERE / "icons" / file).read_bytes()).decode()
    mime = "image/png" if file.endswith(".png") else "image/svg+xml"
    out.append(f'<image x="{x:.1f}" y="{y:.1f}" width="{h * aspect:.1f}" height="{h:.1f}" '
               f'href="data:{mime};base64,{data}"/>')
    return h * aspect


def box(x, y, w, h, fill="#FFFFFF", stroke=LINE, sw=1, rx=8):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
               f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def title(x, y, s, color=NAVY, anchor="middle"):
    title_chars.update(s)
    out.append(f'<text x="{x:.1f}" y="{y}" class="t" fill="{color}" text-anchor="{anchor}">{escape(s)}</text>')


def sub(x, y, s, color=GRAY, anchor="middle", weight=400, rotate=False):
    turn = f' transform="rotate(-90 {x} {y})"' if rotate else ""
    out.append(f'<text x="{x}" y="{y}" font-size="{SUB}" font-weight="{weight}" fill="{color}" '
               f'text-anchor="{anchor}"{turn}>{escape(s)}</text>')


def node(icon, name, x, y, w, *lines, accent=False):
    """Every box: icon + product name (title), then subheading lines. Returns the box height."""
    h = 42 + LINE_GAP * len(lines) + 6
    box(x, y, w, h, stroke=LAVA if accent else LINE, sw=2 if accent else 1)
    cx = x + w / 2
    if name is None:  # the logo is the product name (MLflow wordmark)
        iw = ICONS[icon][1] * 40  # the wordmark has padding: ~40 px tall reads like a 16 px title
        img(icon, cx - iw / 2, y + 2, 40)
    else:
        iw = ICONS[icon][1] * ICON
        left = cx - (iw + 8 + title_width(name)) / 2
        img(icon, left, y + 11, ICON)
        title(left + iw + 8, y + 29, name, anchor="start")
    for i, line in enumerate(lines):
        sub(cx, y + 52 + LINE_GAP * i, line)
    return h


def arrow(d, color=NAVY, width=1.5, dashed=False):
    marker = "lava" if color == LAVA else "navy"
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} '
               f'marker-end="url(#{marker})"/>')


def panel(x, y, w, h, name, subtitle, tint, stroke, center=None):
    box(x, y, w, h, fill=tint, stroke=stroke, rx=14)
    tx, anchor = (center, "middle") if center else (x + 24, "start")
    title(tx, y + 32, name, LAVA, anchor)
    sub(tx, y + 52, subtitle, anchor=anchor)


W, H, TOP = 1200, 935, 44  # TOP: the viewBox starts here

# --- App -------------------------------------------------------------------------------------------
node("apps", "Apps", 360, 60, 480, "Search app · QAC dropdown + SERP results")  # bottom 125

# --- Real-time features (left lane, 150-665) -------------------------------------------------------
panel(40, 150, 520, 515, "REAL-TIME FEATURES", "", "#FFF6F3", "#FFC9BE")
node("kafka", "Apache Kafka", 160, 200, 280, "clickstream events topic")  # 200-265
node("rtm", "Spark Real-Time Mode", 140, 305, 320, "session Feature Views pipeline", accent=True)  # 305-370
node("lakebase", "Lakebase", 140, 450, 320, "online store: session · user · item features",
     "+ QAC prefix completions", accent=True)  # 450-532
arrow("M300 265 V301")
arrow("M300 370 V446", LAVA, 2.5)
sub(316, 405, "< 1 s freshness", LAVA, "start", 700)
sub(316, 422, "event → online store", anchor="start")

# --- L1 retrieval (right lane, 150-450) ------------------------------------------------------------
panel(640, 150, 520, 300, "L1 RETRIEVAL", "candidates from Lakebase and AI Search", "#FFFFFF", "#C9D3D8", center=900)
title(785, 232, "QAC autocomplete")
sub(785, 250, "p95 budget 40 ms")
title(1030, 232, "SERP results")
sub(1030, 250, "p95 budget 400 ms")
node("lakebase", "Lakebase", 665, 265, 210, "prefix lookup")  # 265-330
node("serving", "Model Serving", 925, 265, 210, "query embedding", "GTE · T4 GPU")  # 265-347
node("lakebase", "AI Search", 925, 367, 210, "hybrid search")  # 367-432
arrow("M1030 347 V363")

# --- L2 reranker (right lane, 465-665) -------------------------------------------------------------
panel(640, 465, 520, 200, "L2 RERANKER", "personalized: session + logged-in history", "#F4F7F9", "#C9D3D8",
      center=900)
node("serving", "Model Serving", 665, 535, 210, "LightGBM · CPU", accent=True)  # 535-600
node("serving", "Model Serving", 925, 535, 210, "DCNv2 · T4 GPU", accent=True)
arrow("M770 330 V531")
arrow("M1030 432 V531")

# App -> Kafka and App -> L1
arrow("M420 125 V196")
sub(432, 142, "clicks · searches", anchor="start", weight=500)
arrow("M686 125 V261")
sub(678, 142, "keystroke", anchor="end", weight=500)
arrow("M820 125 V138 H1100 V261")
sub(1108, 168, "search", anchor="start", weight=500)

# L2 -> App: ranked results
arrow("M665 567 H615 V129")
sub(607, 340, "ranked completions", NAVY, weight=500, rotate=True)
arrow("M1135 567 H1180 V92 H844")
sub(1172, 84, "ranked products", NAVY, "end", 500)

# Feature lookup bus: Lakebase -> both L2 endpoints (the point where features meet serving)
arrow("M460 491 H590 V625 H1030 V604", LAVA, 2.5)
arrow("M770 625 V604", LAVA, 2.5)
sub(900, 648, "online feature lookup by session / user / item key", LAVA, weight=600)

# --- Offline training (bottom band, 690-915) -------------------------------------------------------
panel(40, 690, 1120, 225, "OFFLINE TRAINING", "the same clickstream, point-in-time correct", "#FFFFFF", "#C9D3D8")
node("delta", "Delta tables", 125, 780, 190, "clickstream history")  # 780-845
node("feature_store", "Feature Store", 400, 771, 180, "Feature Views", "point-in-time training sets")  # 771-853
node("training", "Model Training", 700, 750, 220, "QAC L2 · LightGBM")  # 750-815
node("ai_runtime", "AI Runtime", 700, 830, 220, "SERP L2 · DCNv2 on GPU")  # 830-895
node("mlflow", None, 960, 785, 180, "model registry")  # 785-850

arrow("M160 232 H24 V812 H121")  # Kafka -> Delta (ingestion pipeline)
sub(30, 804, "ingest", anchor="start", weight=500)
arrow("M315 812 H396")
arrow("M580 797 H650 V782 H696")
arrow("M580 827 H650 V862 H696")
arrow("M920 782 H940 V805 H956")
arrow("M920 862 H940 V835 H956")
arrow("M305 780 V536")  # Delta -> Lakebase: hourly user / item features
sub(316, 610, "hourly user / item features", anchor="start", weight=500)
arrow("M1050 785 V669", dashed=True)  # registry -> serving
sub(1060, 682, "deploy", anchor="start", weight=500)

# Title font: semibold instance subset to the characters used, as WOFF2
opts = subset.Options()
opts.flavor = "woff2"
subsetter = subset.Subsetter(opts)
subsetter.populate(text="".join(sorted(title_chars)))
subsetter.subset(font)
buf = io.BytesIO()
font.flavor = "woff2"
font.save(buf)
font_b64 = base64.b64encode(buf.getvalue()).decode()

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {TOP} {W} {H - TOP}" width="{W}" height="{H - TOP}" font-family="{BODY_FONT}">
<defs>
<style>
@font-face {{ font-family: "DM Sans"; font-weight: 600; src: url(data:font/woff2;base64,{font_b64}) format("woff2"); }}
.t {{ font-family: "DM Sans", {BODY_FONT}; font-size: {TITLE}px; font-weight: 600; }}
</style>
<marker id="navy" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{NAVY}"/></marker>
<marker id="lava" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0 L10 5 L0 10 z" fill="{LAVA}"/></marker>
</defs>
<rect y="{TOP}" width="{W}" height="{H - TOP}" fill="#FFFFFF"/>
{chr(10).join(out)}
</svg>
"""
(HERE / "architecture.svg").write_text(svg)
print("wrote", HERE / "architecture.svg", f"({len(svg) // 1024} KB)")
