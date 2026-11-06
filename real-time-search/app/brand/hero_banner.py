"""Home-page hero banner: "Needle in a brickstack?" as one wide picture in the logo's palette.

A mound of loose bricks with the category icons' tiles half-buried in it (a mound of products), a small needle wedged
into a brick near the peak, and a magnifying glass over it that shows the same scene scaled up, so the needle that's a
sliver in the pile is plain under the glass. Decoration only: the headline is HTML over the left side (App.jsx).

    python app/brand/hero_banner.py [out_dir]   # writes hero.svg (default: app/frontend/public)
"""
import pathlib, random, re, sys

HERE = pathlib.Path(__file__).resolve().parent
ICONS = HERE.parent / "frontend/public/categories"
out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "frontend/public"
out.mkdir(parents=True, exist_ok=True)
N, N6, L, O = "#1B3139", "#1B5162", "#FF3621", "#F9F7F4"
BRICK_TOP, LAVA_TOP = "#2A6478", "#FF6A57"
W, H = 1920, 720

# The mound: ROWS rows of bricks from below the bottom edge up to y ~184, narrowing as (1 - r/ROWS) ** 0.75.
CX, BASE, ROWS, WIDTH, STEP = 1350, 800, 14, 1500, 44
# The magnifying glass: centre, radius and magnification (the needle sits at its centre).
LX, LY, LR, MAG = 1300, 320, 150, 1.4


def icon(name: str) -> str:
    """The inner art of a 96x96 category icon."""
    return re.sub(r"^<svg[^>]*>|</svg>\s*$", "", (ICONS / f"{name}.svg").read_text().strip())


def card(name, cx, cy, size, rot):
    s = size * 0.78 / 96  # icon at 78% of the tile
    return (f'<g transform="rotate({rot} {cx} {cy})" filter="url(#lift)">'
            f'<rect x="{cx - size / 2}" y="{cy - size / 2}" width="{size}" height="{size}" rx="{size * 0.12:.0f}" fill="{O}"/>'
            f'<g transform="translate({cx - 48 * s:.1f} {cy - 48 * s:.1f}) scale({s:.3f})">{icon(name)}</g></g>')


def brick(x, y, rot, lava=False, w=128, h=50):
    face, top = (L, LAVA_TOP) if lava else (N6, BRICK_TOP)
    return (f'<g transform="rotate({rot:.1f} {x} {y})"><rect x="{x - w / 2}" y="{y - h / 2}" width="{w}" height="{h}" '
            f'rx="6" fill="{face}" stroke="{N}" stroke-width="5"/>'
            f'<rect x="{x - w / 2}" y="{y - h / 2}" width="{w}" height="9" rx="4" fill="{top}"/></g>')


def needle(x, y, rot, length):
    """Oat needle from its eye at (x, y), pointing along `rot` degrees."""
    return (f'<g transform="translate({x} {y}) rotate({rot})" filter="url(#lift)">'
            f'<path d="M-26 -12 Q-40 0 -26 12 L{length - 60} 3.6 L{length} 0 L{length - 60} -3.6 Z" fill="{O}"/>'
            f'<rect x="-20" y="-4.2" width="36" height="8.4" rx="4.2" fill="{N}"/></g>')


def surface(x):
    """The mound's top edge at x."""
    d = min(abs(x - CX) / (WIDTH / 2), 1)
    return BASE - STEP * ROWS * (1 - d ** (4 / 3))


# Tiles: (icon, x, depth below the mound's surface, size, rotation), so they follow the mound's shape.
TILES = [(name, x, surface(x) + depth, size, rot) for name, x, depth, size, rot in [
    ("electronics", 880, 140, 150, -9), ("pet-supplies", 1080, 150, 135, 8), ("sports-outdoors", 1230, 300, 150, 6),
    ("clothing-shoes-jewelry", 1560, 110, 140, -5), ("camera-photo", 1700, 170, 145, -10),
    ("grocery-gourmet-food", 1860, 120, 115, 12), ("toys-games", 1450, 330, 130, -6)]]


def mound(seed=21):
    """Drawn from the top row down, so lower (front) bricks overlap what sits above them. Each tile goes in after the
    row behind it, so only the next row or two in front overlap its bottom edge."""
    rnd = random.Random(seed)
    items = {}
    for t in TILES:  # the lowest row drawn before the tile: rows after it start below ~70% of its height
        items.setdefault(max(0, int((BASE - 25 - (t[2] + 0.2 * t[3])) / STEP) + 1), []).append(card(*t))
    rows = []
    for r in range(ROWS):
        y, half = BASE - r * STEP, WIDTH / 2 * (1 - r / ROWS) ** 0.75
        n = max(1, int(2 * half / 104))
        rows.append("".join(brick(CX - half + (i + 0.5) * (2 * half / n) + rnd.uniform(-18, 18), y + rnd.uniform(-8, 8),
                                  rnd.uniform(-14, 14), rnd.random() < 0.05) for i in range(n))
                    + "".join(items.get(r, [])))
    # The needle on top, pointing up-right, its point wedged into a brick lying over it.
    return "".join(reversed(rows)) + needle(LX - 70, LY + 34, -24, 140) + brick(LX + 92, LY - 38, -10)


scene = mound()
svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice">
<defs>
  <filter id="lift" x="-30%" y="-30%" width="160%" height="160%"><feDropShadow dx="0" dy="16" stdDeviation="16" flood-color="#000" flood-opacity="0.4"/></filter>
  <radialGradient id="glass" cx="0.35" cy="0.3" r="0.8"><stop offset="0" stop-color="#fff" stop-opacity="0.16"/><stop offset="1" stop-color="#fff" stop-opacity="0.02"/></radialGradient>
  <clipPath id="lens"><circle cx="{LX}" cy="{LY}" r="{LR}"/></clipPath>
</defs>
<rect width="{W}" height="{H}" fill="{N}"/>
{scene}
<g clip-path="url(#lens)"><rect x="{LX - LR}" y="{LY - LR}" width="{2 * LR}" height="{2 * LR}" fill="{N}"/>
  <g transform="translate({LX} {LY}) scale({MAG}) translate({-LX} {-LY})">{scene}</g>
  <circle cx="{LX}" cy="{LY}" r="{LR}" fill="url(#glass)"/></g>
<path d="M{LX - LR * 0.62} {LY - LR * 0.45} A {LR * 0.78} {LR * 0.78} 0 0 1 {LX - LR * 0.05} {LY - LR * 0.76}" fill="none" stroke="{O}" stroke-width="7" stroke-linecap="round" opacity="0.55"/>
<g filter="url(#lift)"><path d="M{LX + LR * 0.74} {LY + LR * 0.74} L{LX + LR * 1.55} {LY + LR * 1.55}" stroke="{L}" stroke-width="40" stroke-linecap="round"/>
  <circle cx="{LX}" cy="{LY}" r="{LR}" fill="none" stroke="{L}" stroke-width="24"/></g>
<path d="M{LX + LR * 0.83} {LY + LR * 0.83} L{LX + LR * 0.98} {LY + LR * 0.98}" stroke="{N}" stroke-width="40"/>
</svg>
'''
(out / "hero.svg").write_text(svg)
print(out / "hero.svg")
