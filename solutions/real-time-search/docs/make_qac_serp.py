"""Builds docs/qac_serp.svg: a looping animation that shows what QAC and SERP mean, for the top of the README.

Run from real-time-search/: uv run --no-project --with fonttools --with brotli python docs/make_qac_serp.py

A shopper types "wire", the QAC dropdown updates on each keystroke, they click "wireless headphones" and the SERP grid
fades in. It's CSS keyframes only (GitHub renders README SVGs through <img>, which runs CSS animation but no script).
All text is DM Sans (SIL Open Font License) in two weights, subset and inlined, so the caret can be placed by measuring.
With prefers-reduced-motion, it holds one frame: "wire" typed, the dropdown floating over the results, both labels.
"""

import base64
import io
import re
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).parent
ICONS = HERE.parent / "app" / "frontend" / "public" / "categories"
NAVY, GRAY, LAVA, LINES, OAT, OAT_MED, NAVY_300 = (
    "#1B3139", "#5A6F77", "#FF3621", "#DCE0E2", "#F9F7F4", "#EEEDE9", "#C4CCD6")
T = 11.5  # seconds per loop

# --- Story ---------------------------------------------------------------------------------------------------------
TYPED = [(0.6, "w"), (0.9, "wi"), (1.2, "wir"), (1.5, "wire")]  # (time the keystroke lands, box text)
SUGGEST = {  # what the dropdown shows after each keystroke
    "w": ["wireless headphones", "water bottle", "wall charger", "watch band"],
    "wi": ["wireless headphones", "wifi extender", "wireless earbuds", "wireless mouse"],
    "wir": ["wireless headphones", "wireless earbuds", "wireless mouse", "wireless charger"],
    "wire": ["wireless headphones", "wireless earbuds", "wireless mouse", "wireless charger"],
}
POINT, CLICK, SEARCHED = 3.8, 4.5, 4.75  # pointer arrives on row 1, presses, results load
END = 10.9  # both labels hold together until here, then everything fades out by T
PICKED = SUGGEST["wire"][0]
TILES = [  # (title line 1, title line 2, brand, price)
    ("Noise Canceling", "Headphones", "Sony", "$278"),
    ("Over-Ear Wireless", "Headphones", "Soundcore", "$36"),
    ("Solo3 Wireless", "On-Ear", "Beats", "$129"),
    ("Wireless Earbuds", "+ Charging Case", "JLab", "$25"),
]

# --- Layout --------------------------------------------------------------------------------------------------------
W, H = 960, 392
BW, BH, BY = 552, 46, 44
BX = W - 48 - BW  # search box, against the right edge
ROW = 34  # dropdown row height
DY = BY + BH + 6  # dropdown top
GY = BY + BH + 48  # grid top
TW = (BW - 3 * 12) / 4  # tile width
LX = BX - 48  # labels: right-aligned to here

fonts = {w: instancer.instantiateVariableFont(TTFont(HERE / "fonts" / "DMSans-VariableFont.ttf"),
                                              {"wght": w, "opsz": 14}) for w in (400, 600)}
used = {400: set(), 600: set()}
out, keyframes = [], []


def width(s, size, w=400):
    f = fonts[w]
    cmap, hmtx = f.getBestCmap(), f["hmtx"]
    return sum(hmtx[cmap[ord(c)]][0] for c in s) * size / f["head"].unitsPerEm


def text(x, y, s, size, color=NAVY, w=400, anchor="start", extra=""):
    """s may contain <b>…</b> for a semibold run (the typed prefix in a completion)."""
    parts = re.split(r"<b>(.*?)</b>", s)
    spans = []
    for i, p in enumerate(parts):
        weight = 600 if i % 2 else w
        used[weight].update(p)
        spans.append(f'<tspan font-weight="{weight}">{escape(p)}</tspan>' if weight != w else escape(p))
    used[w].update(" ")
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{w}" fill="{color}" '
            f'text-anchor="{anchor}"{extra}>{"".join(spans)}</text>')


def visible(t0, t1, fade=0.0, still=False):
    """A class that shows its element from t0 to t1 each loop (fading over `fade` s), plus its reduced-motion state."""
    name = f"k{len(keyframes)}"
    pct = lambda t: f"{max(0.0, min(100.0, t / T * 100)):.3f}%"
    stops = [(0, 0), (t0, 0), (t0 + max(fade, 0.01), 1), (t1 - max(fade, 0.01), 1), (t1, 0), (T, 0)]
    frames = " ".join(f"{pct(t)} {{ opacity: {o}; }}" for t, o in stops)
    keyframes.append(f"@keyframes {name} {{ {frames} }}\n"
                     f".{name} {{ opacity: 0; animation: {name} {T}s linear infinite; }}\n"
                     f"@media (prefers-reduced-motion: reduce) {{ .{name} {{ animation: none; opacity: {int(still)}; }} }}")
    return name


def group(cls, *items, extra=""):
    out.append(f'<g class="{cls}"{extra}>' + "".join(items) + "</g>")


def label(y, tag, name, line, cls, tick_cls=None):
    """A callout: lava tag + full name + one line of plain English, right-aligned, with a tick pointing right at the UI.
    The tick can have its own timing (`tick_cls`), so it can leave with the UI it points at while the label stays."""
    tw = width(tag, 15, 600) + 22
    group(tick_cls or cls,
          f'<path d="M{LX + 10} {y + 14} H{LX + 40}" stroke="{LAVA}" stroke-width="2" stroke-dasharray="4 4"/>',
          f'<circle cx="{LX + 40}" cy="{y + 14}" r="3.5" fill="{LAVA}"/>')
    group(cls,
          f'<rect x="{LX - tw:.1f}" y="{y}" width="{tw:.1f}" height="28" rx="14" fill="{LAVA}"/>',
          text(LX - tw / 2, y + 19, tag, 15, "#FFFFFF", 600, "middle"),
          text(LX, y + 54, name, 17, NAVY, 600, "end"),
          text(LX, y + 76, line[0], 14, GRAY, anchor="end"),
          text(LX, y + 95, line[1], 14, GRAY, anchor="end"))


# Card on the oat page, like the app's storefront
out.append(f'<rect width="{W}" height="{H}" fill="{OAT}"/>')
out.append(f'<rect x="16.5" y="16.5" width="{W - 33}" height="{H - 33}" rx="12" fill="#FFFFFF" stroke="{LINES}"/>')
everything_start = len(out)

# Search box, magnifier, placeholder
out.append(f'<rect x="{BX + .5}" y="{BY + .5}" width="{BW}" height="{BH}" rx="4" fill="#FFFFFF" stroke="{LINES}"/>')
group(visible(0.3, T, 0.15, still=True), f'<rect x="{BX - .5}" y="{BY - .5}" width="{BW + 2}" height="{BH + 2}" rx="5" '
      f'fill="none" stroke="{LAVA}" stroke-width="2"/>')  # focus ring
out.append(f'<g fill="none" stroke="{GRAY}" stroke-width="2.2" stroke-linecap="round">'
           f'<circle cx="{BX + 22}" cy="{BY + 21}" r="6.5"/><path d="M{BX + 27} {BY + 26} L{BX + 31} {BY + 30}"/></g>')
TX, TY = BX + 44, BY + 29  # box text baseline
group(visible(0, TYPED[0][0]), text(TX, TY, "Search products", 17, NAVY_300))

# Typed text, one state per keystroke, then the picked completion; a blinking caret follows each
states = TYPED + [(SEARCHED, PICKED)]
for i, (t0, s) in enumerate(states):
    t1 = states[i + 1][0] if i + 1 < len(states) else END
    group(visible(t0, t1, still=s == "wire"), text(TX, TY, s, 17))
caret_at = [(0.3, 0.0)] + [(t0, width(s, 17)) for t0, s in TYPED]
for i, (t0, dx) in enumerate(caret_at):
    t1 = caret_at[i + 1][0] if i + 1 < len(caret_at) else SEARCHED
    group(visible(t0, t1), f'<rect class="blink" x="{TX + dx + 1:.1f}" y="{BY + 13}" width="1.6" height="21" fill="{NAVY}"/>')

# SERP: a heading and a row of product tiles, staggered in, behind where the dropdown floats
serp = []
icon = re.sub(r"^<svg[^>]*>|</svg>\s*$", "", (ICONS / "electronics.svg").read_text().strip())
for i, (l1, l2, brand, price) in enumerate(TILES):
    x = BX + i * (TW + 12)
    assert max(width(l1, 12.5), width(l2, 12.5)) <= TW - 20, f"tile title too wide: {l1} / {l2}"
    t0 = SEARCHED + 0.15 + 0.12 * i
    th = TW - 20  # square thumb
    group(visible(t0, END, 0.3, still=True),
          f'<rect x="{x + .5:.1f}" y="{GY + .5}" width="{TW:.1f}" height="{th + 98:.1f}" rx="8" fill="#FFFFFF" stroke="{LINES}"/>',
          f'<rect x="{x + 10:.1f}" y="{GY + 10}" width="{th:.1f}" height="{th:.1f}" rx="6" fill="{OAT_MED}"/>',
          f'<svg x="{x + 10 + th * 0.19:.1f}" y="{GY + 10 + th * 0.19:.1f}" width="{th * 0.62:.1f}" height="{th * 0.62:.1f}" '
          f'viewBox="0 0 96 96">{icon}</svg>',
          *(text(x + 10, GY + th + 30 + 16 * j, line, 12.5) for j, line in enumerate((l1, l2))),
          text(x + 10, GY + th + 63, brand, 11.5, GRAY),
          text(x + 10, GY + th + 84, price, 16, NAVY, 600))
group(visible(SEARCHED + 0.1, END, 0.3, still=True),
      text(BX, GY - 14, f"Results for <b>{PICKED}</b>", 14, GRAY))

label(GY + 100, "SERP", "Search Engine Results Page", ("ranks products for the query", "once it's submitted"),
      visible(SEARCHED + 0.7, END, 0.3, still=True))

# QAC dropdown: one panel; its rows change on each keystroke and row 1 lights up when the pointer lands on it
dh = 12 + ROW * 4
dropdown = [f'<rect x="{BX}" y="{DY}" width="{BW}" height="{dh}" rx="8" fill="#FFFFFF" stroke="{LINES}" '
            f'filter="url(#shadow)"/>']
group(visible(TYPED[0][0] + 0.08, SEARCHED, 0.08, still=True), *dropdown)
group(visible(POINT + 0.15, SEARCHED, 0.05, still=True),
      f'<rect x="{BX + 8}" y="{DY + 6}" width="{BW - 16}" height="{ROW}" rx="4" fill="{OAT_MED}" '
      f'stroke="{LAVA}" stroke-width="2"/>')
for i, (t0, prefix) in enumerate(TYPED):
    t1 = TYPED[i + 1][0] if i + 1 < len(TYPED) else SEARCHED
    rows = []
    for r, q in enumerate(SUGGEST[prefix]):
        y = DY + 6 + ROW * r + 22
        rows.append(f'<g fill="none" stroke="{NAVY_300}" stroke-width="1.8" stroke-linecap="round">'
                    f'<circle cx="{BX + 26}" cy="{y - 5}" r="4.5"/><path d="M{BX + 29.5} {y - 1.5} L{BX + 32} {y + 1}"/></g>')
        rows.append(text(BX + 44, y, f"<b>{escape(q[:len(prefix)])}</b>{escape(q[len(prefix):])}", 15))
    group(visible(t0 + 0.08, t1, 0.08 if i == 0 or i == len(TYPED) - 1 else 0, still=prefix == "wire"), *rows)

label(DY + 22, "QAC", "Query Auto-Completion", ("suggests whole queries as you type,", "on every keystroke"),
      visible(1.0, END, 0.25, still=True), tick_cls=visible(1.0, SEARCHED, 0.25, still=True))

# Pointer: glides in from the right, lands on row 1, presses, then leaves
px, py = BX + 236, DY + 6 + ROW / 2 + 2
pointer = (f'<path d="M0 0 L0 17 L4.5 12.8 L7.6 19.6 L10.4 18.4 L7.4 11.6 L13 11.6 Z" fill="{NAVY}" '
           f'stroke="#FFFFFF" stroke-width="1.4" stroke-linejoin="round"/>')
p = lambda t: f"{t / T * 100:.3f}%"
keyframes.append(
    f"@keyframes glide {{ 0%, {p(POINT - 0.6)} {{ transform: translate(140px, 60px); opacity: 0; }} "
    f"{p(POINT - 0.45)} {{ opacity: 1; }} {p(POINT)} {{ transform: translate(0, 0); }} "
    f"{p(CLICK - 0.08)} {{ transform: translate(0, 0) scale(1); }} {p(CLICK)} {{ transform: translate(0, 0) scale(0.82); }} "
    f"{p(CLICK + 0.12)} {{ transform: translate(0, 0) scale(1); opacity: 1; }} "
    f"{p(SEARCHED + 0.3)}, 100% {{ transform: translate(0, 0); opacity: 0; }} }}\n"
    f".pointer {{ animation: glide {T}s ease-out infinite; transform-box: fill-box; opacity: 0; }}\n"
    f"@media (prefers-reduced-motion: reduce) {{ .pointer {{ animation: none; }} }}")
out.append(f'<g transform="translate({px} {py})"><g class="pointer">{pointer}</g></g>')

# Fade the whole card's contents at the end of the loop so it restarts from a clean box
body = out[everything_start:]
del out[everything_start:]
keyframes.append(f"@keyframes loop {{ 0%, {p(END)} {{ opacity: 1; }} {p(T - 0.15)}, 100% {{ opacity: 0; }} }}\n"
                 f".loop {{ animation: loop {T}s linear infinite; }}\n"
                 f"@media (prefers-reduced-motion: reduce) {{ .loop {{ animation: none; }} }}")
out.append('<g class="loop">' + "".join(body) + "</g>")

# Fonts: each weight subset to the characters it draws, as WOFF2
faces = []
for w, chars in used.items():
    opts = subset.Options()
    opts.flavor = "woff2"
    s = subset.Subsetter(opts)
    s.populate(text="".join(sorted(chars)))
    s.subset(fonts[w])
    buf = io.BytesIO()
    fonts[w].flavor = "woff2"
    fonts[w].save(buf)
    faces.append(f'@font-face {{ font-family: "DM Sans"; font-weight: {w}; '
                 f'src: url(data:font/woff2;base64,{base64.b64encode(buf.getvalue()).decode()}) format("woff2"); }}')

nl = "\n"
svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="'DM Sans', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif">
<title>QAC suggests whole queries as you type; SERP ranks products once you search</title>
<defs>
<filter id="shadow" x="-10%" y="-10%" width="120%" height="140%"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="{NAVY}" flood-opacity="0.14"/></filter>
<style>
{nl.join(faces)}
@keyframes blink {{ 0%, 49% {{ opacity: 1; }} 50%, 100% {{ opacity: 0; }} }}
.blink {{ animation: blink 0.9s step-end infinite; }}
{nl.join(keyframes)}
</style>
</defs>
{nl.join(out)}
</svg>
"""
(HERE / "qac_serp.svg").write_text(svg)
print("wrote", HERE / "qac_serp.svg", f"({len(svg) // 1024} KB)")
