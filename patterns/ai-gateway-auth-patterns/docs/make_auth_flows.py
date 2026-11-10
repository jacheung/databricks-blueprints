"""Builds docs/auth_routes.svg: the two ways an external app reaches AI Gateway, side by side, each read top-down
(caller → your cloud → Databricks).

Run from ai-gateway-auth-patterns/: uv run --no-project --with fonttools --with brotli python docs/make_auth_flows.py

Same conventions as real-time-search/docs/make_*.py (brand palette, DM Sans titles subset and inlined, icons as data
URIs, moving dashed flows): GitHub renders README SVGs through <img>, which won't load external files or run script.
~1200 px wide, so GitHub scales it to ~85% and the 12 px labels stay legible. Badges number the phases in README.md's
table. Fails if a title or subheading is wider than its box.
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
TITLE, SUB = 16, 12
ICON, LINE_GAP = 24, 17
SUB_CHAR = 6.4  # generous average glyph width of the 12 px system font, for the overflow check

ICONS = {"serving": ("primary-icon-orange-productionize.svg", 1.0)}  # name -> (file, width / height)

FLOWS = {  # brand palette: Navy 500, Yellow 600, Blue 500; color = whose identity the hop carries
    "request": ("#618794", "request / response"),
    "sp": ("#FFAB00", "SP identity"),
    "user": ("#4299E0", "user identity"),
}

CW, GAP = 570, 60  # route column width, gap between the columns (the divider runs down its middle)
W, H = 2 * CW + GAP, 725
HEAD = 4  # arrowheads end this far before a box edge
K0, C0, D0 = 100, 305, 495  # caller, cloud, Databricks panel tops

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
    out.append(f'<image x="{x:.1f}" y="{y:.1f}" width="{h * aspect:.1f}" height="{h:.1f}" '
               f'href="data:image/svg+xml;base64,{data}"/>')


def box(x, y, w, h, fill="#FFFFFF", stroke=LINE, sw=1, rx=8):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
               f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')


def title(x, y, s, color=NAVY, anchor="middle", size=None):
    title_chars.update(s)
    style = f' style="font-size: {size}px"' if size else ""
    out.append(f'<text x="{x:.1f}" y="{y}" class="t"{style} fill="{color}" text-anchor="{anchor}">{escape(s)}</text>')


def sub(x, y, s, color=GRAY, anchor="middle", weight=400):
    out.append(f'<text x="{x}" y="{y}" font-size="{SUB}" font-weight="{weight}" fill="{color}" '
               f'text-anchor="{anchor}">{escape(s)}</text>')


def label(x, y, s, anchor="middle"):
    sub(x, y, s, NAVY, anchor, 500)


def height(n):
    return 42 + LINE_GAP * n + 6


def at(mid, h):
    """Top y of a box of height h centered on mid."""
    return mid - h / 2


def node(icon, name, x, y, w, *lines, h=None):
    """A box: icon (optional) + name, then subheading lines, centered vertically in h.
    Returns (top, bottom, middle y, left, right)."""
    h = h or height(len(lines))
    box(x, y, w, h)
    cx, pad = x + w / 2, (h - height(len(lines))) / 2
    iw = ICONS[icon][1] * ICON if icon else 0
    gap = 8 if icon else 0
    need = iw + gap + title_width(name)
    assert need <= w - 16, f"title {name!r} needs {need:.0f} px in a {w} px box"
    left = cx - need / 2
    if icon:
        img(icon, left, y + pad + 11, ICON)
    title(left + iw + gap, y + pad + 29, name, anchor="start")
    for i, line in enumerate(lines):
        assert len(line) * SUB_CHAR <= w - 12, f"subheading {line!r} is too wide for a {w} px box"
        sub(cx, y + pad + 52 + LINE_GAP * i, line)
    return y, y + h, y + h / 2, x, x + w


def panel(x, y, w, h, name, subtitle, tint="#FFFFFF", stroke="#C9D3D8"):
    box(x, y, w, h, fill=tint, stroke=stroke, rx=14)
    title(x + 20, y + 30, name, LAVA, "start")
    sub(x + 20, y + 49, subtitle, anchor="start")


def arrow(d, flow):
    out.append(f'<path d="{d}" class="flow" fill="none" stroke="{FLOWS[flow][0]}" stroke-width="2.5" '
               f'marker-end="url(#{flow})"/>')


def varrow(x, y1, y2, flow):
    arrow(f"M{x} {y1} V{y2 - HEAD if y2 > y1 else y2 + HEAD}", flow)


def step(x, y, n, flow):
    """A numbered badge on an arrow: the phase (row) in the README's table."""
    out.append(f'<circle cx="{x}" cy="{y}" r="10" fill="#FFFFFF" stroke="{FLOWS[flow][0]}" stroke-width="2"/>')
    title(x, y + 4.5, str(n), size=12)


def column(ox, obo):
    """One route, read top-down. ox: the column's left edge."""
    title(ox + 10, 38, "Route 2: on-behalf-of (OBO) via an SP" if obo else "Route 1: service principal (M2M)",
          anchor="start", size=20)
    sub(ox + 10, 62, "The app trades the user's ID token for a Databricks token that carries the user." if obo
        else "The app authenticates as the SP: every request reaches AI Gateway as one identity.", anchor="start")
    me = "user" if obo else "sp"  # the identity the app's Databricks token carries

    # --- Caller ---------------------------------------------------------------------------------------------------
    panel(ox, K0, CW, 165, "USER + IdP" if obo else "CALLER",
          "Google · Okta · IAP · etc." if obo else "a user or an agent")
    user = node(None, "User" if obo else "User / Agent", ox + 280, at(K0 + 100, height(1)), 200, "calls your app")
    if obo:
        idp = node(None, "Identity provider", ox + 15, at(K0 + 100, height(2)), 200, "issues the user's ID token",
                   "registered with Databricks")
        y, mid = K0 + 100, (idp[4] + user[3]) / 2
        arrow(f"M{user[3]} {y - 10} H{idp[4] + HEAD}", "user")
        arrow(f"M{idp[4]} {y + 10} H{user[3] - HEAD}", "user")
        step(mid, y, 1, "user")
        label(mid, y - 20, "sign in")
        label(mid, y + 34, "ID token")

    # --- Your cloud -----------------------------------------------------------------------------------------------
    panel(ox, C0, CW, 150, "YOUR CLOUD", "Cloud Run · Lambda · GKE · etc.")
    app = node(None, "Application", ox + 235, C0 + 15, 320,
               *(["holds the SP credentials and", "exchanges each user's ID token"] if obo
                 else ["authenticates as the SP", "with client credentials"]), h=120)
    secret = node(None, "Secret store", ox + 15, C0 + 62, 170, "SP client_id + secret")
    arrow(f"M{secret[4]} {secret[2]} H{app[3] - HEAD}", "sp")
    step((secret[4] + app[3]) / 2, secret[2], 3, "sp")

    varrow(ox + 360, user[1], app[0], me if obo else "request")  # in OBO the request carries the user's ID token
    varrow(ox + 400, app[0], user[1], "request")
    mid = (user[1] + app[0]) / 2
    step(ox + 360, mid, 2, me if obo else "request")
    label(ox + 344, mid + 4, "request + ID token" if obo else "request", anchor="end")
    label(ox + 416, mid + 4, "response", anchor="start")

    # --- Databricks -----------------------------------------------------------------------------------------------
    panel(ox, D0, CW, 180, "DATABRICKS", "token endpoint · AI Gateway", "#FFF6F3", "#FFC9BE")
    token = node(None, "Token endpoint", ox + 15, D0 + 62, 270,
                 *(["/oidc/v1/token · token exchange", "SP credentials valid?", "JWT email → a Databricks user?"]
                   if obo else ["/oidc/v1/token", "grant: client_credentials"]))
    gateway = node("serving", "AI Gateway", ox + 305, D0 + 62, 250,
                   *(["runs as the user", "per-user limits · audit · cost"] if obo
                     else ["runs as the SP", "one identity for every caller"]))
    if obo:  # both identities go up at once: the SP authenticates, the user's JWT is the subject
        varrow(ox + 244, app[1], token[0], "sp")
        varrow(ox + 258, app[1], token[0], "user")
        step(ox + 251, (app[1] + token[0]) / 2, 4, "user")
        varrow(ox + 278, token[0], app[1], "user")
    else:
        varrow(ox + 250, app[1], token[0], "sp")
        step(ox + 250, (app[1] + token[0]) / 2, 4, "sp")
        varrow(ox + 272, token[0], app[1], "sp")
    label(ox + 230, C0 + 168, "↓ SP creds + user JWT" if obo else "↓ client_id + secret", anchor="end")
    label(ox + 230, C0 + 185, "↑ token as the user" if obo else "↑ token as the SP", anchor="end")
    varrow(ox + 400, app[1], gateway[0], me)
    step(ox + 400, (app[1] + gateway[0]) / 2, 5, me)
    varrow(ox + 425, gateway[0], app[1], "request")
    label(ox + 440, C0 + 168, "↓ OBO token" if obo else "↓ SP token", anchor="start")
    label(ox + 440, C0 + 185, "↑ response", anchor="start")


column(0, obo=False)
column(CW + GAP, obo=True)
out.append(f'<path d="M{W / 2} 20 V{H - 50}" stroke="#C9D3D8" stroke-width="2" stroke-dasharray="8 6"/>')

# --- Legend: one row along the bottom, shared by both routes ----------------------------------------------------------
widths = [50 + title_width(text, 14) + 40 for _, text in FLOWS.values()]
x = (W - sum(widths) + 40) / 2
for (color, text), wd in zip(FLOWS.values(), widths):
    out.append(f'<path d="M{x:.0f} {H - 24.5} H{x + 38:.0f}" fill="none" stroke="{color}" stroke-width="3.5" '
               f'stroke-dasharray="8 6"/>')
    title(x + 50, H - 20, text, anchor="start", size=14)
    x += wd

# --- Font + write -----------------------------------------------------------------------------------------------------
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
    f'<marker id="{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4.5" markerHeight="4.5" orient="auto">'
    f'<path d="M0 0 L10 5 L0 10 z" fill="{color}"/></marker>' for k, (color, _) in FLOWS.items())

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
(HERE / "auth_routes.svg").write_text(svg)
print("wrote", HERE / "auth_routes.svg", f"({len(svg) // 1024} KB)")
