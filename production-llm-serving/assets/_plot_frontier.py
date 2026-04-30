import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib import rcParams

# ── Databricks palette ─────────────────────────────────────────────────────────
BG          = "#F9F7F4"   # Oat Light — main surface
INK         = "#1B3139"   # Navy 800 — text, axes, labels
GRAY_TEXT   = "#5A6F77"   # Gray Text — secondary / muted
GRAY_LINES  = "#DCE0E2"   # Gray Lines — grid, borders

# Lava sequential scale (light → dark = less → more capacity)
LAVA_300    = "#FABFBA"   # 1× PT (lightest)
LAVA_600    = "#FF3621"   # 2× PT (primary)
LAVA_800    = "#801C17"   # 2× PT + PPT (darkest)

# ── Capacity parameters ────────────────────────────────────────────────────────
PT_1X_ITPM, PT_1X_OTPM = 400_000, 100_000
PT_2X_ITPM, PT_2X_OTPM = 800_000, 200_000
PPT_ITPM,   PPT_OTPM   = 200_000,  10_000

COMB_MAX_Y  = PT_2X_OTPM + PPT_OTPM
COMB_KINK_X = PPT_ITPM
COMB_KINK_Y = PT_2X_OTPM
COMB_MAX_X  = PT_2X_ITPM + PPT_ITPM
SLOPE2      = -COMB_KINK_Y / (COMB_MAX_X - COMB_KINK_X)

workloads = [
    {"name": "Document Q&A",   "tokens": "2,000 in / 200 out",
     "input": 2_000, "output":   200, "color": INK,       "marker": "o",
     "ann_pos": (0.53, 0.18), "ann_rad":  0.15},
    {"name": "Code generation", "tokens": "200 in / 2,000 out",
     "input":   200, "output": 2_000, "color": GRAY_TEXT, "marker": "s",
     "ann_pos": (0.11, 0.65), "ann_rad": -0.10},
]

MAX_X, MAX_Y = COMB_MAX_X, COMB_MAX_Y

rcParams["font.family"] = "sans-serif"
fig, ax = plt.subplots(figsize=(12, 7))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
for spine in ax.spines.values():
    spine.set_edgecolor(GRAY_LINES)
    spine.set_linewidth(0.8)
ax.tick_params(colors=INK, labelsize=9.5)

# ── Region fills ───────────────────────────────────────────────────────────────
x_r1 = np.linspace(0, PT_1X_ITPM, 400)
ax.fill_between(x_r1, PT_1X_OTPM * (1 - x_r1 / PT_1X_ITPM), 0,
                alpha=0.18, color=LAVA_300, zorder=1)

x_r2a = np.linspace(0, PT_1X_ITPM, 400)
ax.fill_between(x_r2a,
                PT_1X_OTPM * (1 - x_r2a / PT_1X_ITPM),
                PT_2X_OTPM * (1 - x_r2a / PT_2X_ITPM),
                alpha=0.14, color=LAVA_600, zorder=1)
x_r2b = np.linspace(PT_1X_ITPM, PT_2X_ITPM, 400)
ax.fill_between(x_r2b, 0, PT_2X_OTPM * (1 - x_r2b / PT_2X_ITPM),
                alpha=0.14, color=LAVA_600, zorder=1)

x_r3a = np.linspace(0, COMB_KINK_X, 400)
ax.fill_between(x_r3a,
                PT_2X_OTPM * (1 - x_r3a / PT_2X_ITPM),
                COMB_MAX_Y - (PPT_OTPM / PPT_ITPM) * x_r3a,
                alpha=0.18, color=LAVA_800, zorder=1)
x_r3b = np.linspace(COMB_KINK_X, PT_2X_ITPM, 400)
ax.fill_between(x_r3b,
                PT_2X_OTPM * (1 - x_r3b / PT_2X_ITPM),
                COMB_KINK_Y + SLOPE2 * (x_r3b - COMB_KINK_X),
                alpha=0.18, color=LAVA_800, zorder=1)
x_r3c = np.linspace(PT_2X_ITPM, COMB_MAX_X, 400)
ax.fill_between(x_r3c, 0, COMB_KINK_Y + SLOPE2 * (x_r3c - COMB_KINK_X),
                alpha=0.18, color=LAVA_800, zorder=1)

# ── Region labels ──────────────────────────────────────────────────────────────
label_box = dict(boxstyle="round,pad=0.45", fc=BG, lw=1.1)

ax.text(115_000, 22_000, "Provisioned\nThroughput",
        color=INK, fontsize=9.5, ha="center", va="center",
        linespacing=1.5, fontweight="semibold",
        bbox={**label_box, "ec": LAVA_300})

ax.text(310_000, 65_000,
        "Provisioned Throughput\nBurst Capacity\n(*not guaranteed)",
        color=INK, fontsize=9.5, ha="center", va="center", linespacing=1.5,
        fontweight="semibold",
        bbox={**label_box, "ec": LAVA_600})

ax.text(510_000, 108_000, "Pay-Per-Token",
        color=INK, fontsize=9.5, ha="center", va="center",
        fontweight="semibold",
        bbox={**label_box, "ec": LAVA_800})

# ── Frontier lines ─────────────────────────────────────────────────────────────
ax.plot([0, PT_1X_ITPM], [PT_1X_OTPM, 0],
        color=LAVA_300, lw=2.5, zorder=4,
        label=f"1× PT  (ITPM {PT_1X_ITPM//1000:,}k / OTPM {PT_1X_OTPM//1000:,}k)")

ax.plot([0, PT_2X_ITPM], [PT_2X_OTPM, 0],
        color=LAVA_600, lw=2.5, zorder=4,
        label=f"2× PT  (ITPM {PT_2X_ITPM//1000:,}k / OTPM {PT_2X_OTPM//1000:,}k)")

ax.plot([0, COMB_KINK_X, COMB_MAX_X], [COMB_MAX_Y, COMB_KINK_Y, 0],
        color=LAVA_800, lw=2.5, zorder=4,
        label=f"2× PT + PPT  (ITPM {COMB_MAX_X//1000:,}k / OTPM {COMB_MAX_Y//1000:,}k)")

ax.scatter([COMB_KINK_X], [COMB_KINK_Y], color=LAVA_800, s=55,
           marker="D", zorder=5, edgecolors=BG, lw=1.2)

# ── Per-workload operating points ──────────────────────────────────────────────
def combined_intersect(n_in, n_out):
    slope_wl = n_out / n_in
    itpm1 = COMB_MAX_Y / (slope_wl + PPT_OTPM / PPT_ITPM)
    if 0 <= itpm1 <= COMB_KINK_X:
        return itpm1, slope_wl * itpm1
    x = (COMB_KINK_Y - SLOPE2 * COMB_KINK_X) / (slope_wl - SLOPE2)
    return x, slope_wl * x

for wl in workloads:
    n_in, n_out = wl["input"], wl["output"]
    color = wl["color"]

    qpm1 = 1 / (n_in / PT_1X_ITPM + n_out / PT_1X_OTPM)
    x1, y1 = qpm1 * n_in, qpm1 * n_out

    qpm2 = 1 / (n_in / PT_2X_ITPM + n_out / PT_2X_OTPM)
    x2, y2 = qpm2 * n_in, qpm2 * n_out

    x3, y3 = combined_intersect(n_in, n_out)
    qpm3 = x3 / n_in

    ax.plot([x1, x1], [0,  y1], color=color, lw=0.9, ls="--", alpha=0.35, zorder=2)
    ax.plot([0,  x1], [y1, y1], color=color, lw=0.9, ls="--", alpha=0.35, zorder=2)
    ax.plot([x1, x2, x3], [y1, y2, y3], color=color, lw=0.9, ls=":", alpha=0.35, zorder=2)

    for x, y, s in [(x1, y1, 95), (x2, y2, 125), (x3, y3, 165)]:
        ax.scatter(x, y, color=color, marker=wl["marker"],
                   s=s, zorder=5, edgecolors=BG, lw=1.5)

    is_near_yaxis = x1 < MAX_X * 0.05
    for x, y, qpm, fw in [(x1, y1, qpm1, "normal"),
                           (x2, y2, qpm2, "normal"),
                           (x3, y3, qpm3, "semibold")]:
        if is_near_yaxis:
            ax.text(x + MAX_X * 0.013, y, f"{qpm:.0f} QPM",
                    color=color, fontsize=9, ha="left", va="center", fontweight=fw)
        else:
            ax.text(x, y + MAX_Y * 0.028, f"{qpm:.0f} QPM",
                    color=color, fontsize=9, ha="center", va="bottom", fontweight=fw)

    ax.annotate(
        f"{wl['name']}\n({wl['tokens']})",
        xy=(x3, y3),
        xytext=wl["ann_pos"], textcoords="axes fraction",
        fontsize=10, color=color,
        arrowprops=dict(arrowstyle="->", color=color, lw=1.2,
                        connectionstyle=f"arc3,rad={wl['ann_rad']}"),
        bbox=dict(boxstyle="round,pad=0.4", fc=BG, ec=color, alpha=0.97, lw=1.2),
        zorder=6,
    )

# ── Axes + legend ──────────────────────────────────────────────────────────────
ax.set_xlabel("Input Tokens per Minute (ITPM)", fontsize=11, color=INK, labelpad=8)
ax.set_ylabel("Output Tokens per Minute (OTPM)", fontsize=11, color=INK, labelpad=8)
ax.set_title(
    "Capacity Frontiers\n"
    "PT at 4:1 ratio  |  PPT (GPT OSS 120b) at 20:1 ratio",
    fontsize=11.5, pad=14, color=INK,
)
ax.set_xlim(-MAX_X * 0.02, MAX_X * 1.06)
ax.set_ylim(-MAX_Y * 0.08, MAX_Y * 1.14)
ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
ax.grid(True, alpha=0.5, linestyle=":", color=GRAY_LINES)
ax.legend(fontsize=10, loc="upper right", framealpha=1.0,
          facecolor=BG, edgecolor=GRAY_LINES, labelcolor=INK)

plt.tight_layout()
out = "/Users/jon.cheung/GitHub/databricks-blueprints/production-llm-serving/pt_capacity_frontier.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG)
print(f"Saved to {out}")
