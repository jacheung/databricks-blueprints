"""Rebuild the readout charts, flow diagram, and PDF from a latency results CSV.

Usage:
    uv run --no-project --with matplotlib --with markdown -- \
        python analysis/build_readout.py analysis/latency_comparison_<run_id>_results.csv
"""

import argparse
import csv
import statistics
import subprocess
import tempfile
from pathlib import Path

import markdown
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


ANALYSIS_DIR = Path(__file__).resolve().parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
POLICIES = [  # Fixed categorical order and colors.
    ("block_jailbreak", "Jailbreak", "#2a78d6"),
    ("block_unsafe_content", "Unsafe content", "#eb6834"),
    ("detect_sensitive_data", "Sensitive data", "#1baf7a"),
    ("block_hallucination", "Hallucination", "#eda100"),
]
SURFACE, TEXT, TEXT2, GRID, LINE = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#8a8984"
BLOCK = "#b42318"
# Label side per policy, chosen so labels clear the error bars.
SIDES = {
    "latency": {"block_jailbreak": "below", "block_unsafe_content": "left",
                "detect_sensitive_data": "right", "block_hallucination": "right"},
    "accuracy": {"block_jailbreak": "above", "block_unsafe_content": "left",
                 "detect_sensitive_data": "below", "block_hallucination": "left"},
}
PDF_CSS = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; color: #0b0b0b;
       font-size: 10.5pt; line-height: 1.45; max-width: 7.5in; margin: 0 auto; }
h1 { font-size: 20pt; margin-bottom: 4pt; } h2 { font-size: 14pt; margin-top: 18pt; }
h3 { font-size: 11.5pt; } code { font-size: 9pt; background: #f1f0ec; padding: 0 2px; }
table { border-collapse: collapse; width: 100%; font-size: 8.5pt; margin: 8pt 0; }
th, td { border: 1px solid #e4e3df; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: #f6f5f1; } img { max-width: 100%; display: block; margin: 8pt auto; }
img[alt^="End-to-end"], img[alt^="Accuracy"] { max-width: 72%; }
h2, h3, img, table { page-break-inside: avoid; }
"""


def policy_stats(csv_path):
    """Mean ± SE per (arm, policy) over prompt means; reps of a prompt are not independent."""
    per_prompt = {}
    with open(csv_path, newline="", encoding="utf-8") as results_file:
        for row in csv.DictReader(results_file):
            if row["arm"] not in ("service_policies", "ai_decide_split"):
                continue
            entry = per_prompt.setdefault((row["arm"], row["policy"], row["query_id"]), ([], []))
            entry[0].append(float(row["total_ms"]) / 1000)
            if row["classification"]:
                entry[1].append(100.0 if row["classification"] in ("TP", "TN") else 0.0)
    grouped = {}
    for (arm, policy, _), (seconds, accuracy) in per_prompt.items():
        means = grouped.setdefault((arm, policy), ([], []))
        means[0].append(statistics.mean(seconds))
        means[1].append(statistics.mean(accuracy))

    def mean_se(values):
        return statistics.mean(values), statistics.stdev(values) / len(values) ** 0.5

    return {key: (*mean_se(seconds), *mean_se(accuracy)) for key, (seconds, accuracy) in grouped.items()}


def scatter(stats, kind, lo, hi, unit, title, region_text, legend_loc, path):
    value, error = (0, 1) if kind == "latency" else (2, 3)
    fig, ax = plt.subplots(figsize=(5.6, 5.4), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.plot([lo, hi], [lo, hi], color=TEXT2, lw=1, ls=(0, (4, 3)), zorder=1)
    ax.text(lo + (hi - lo) * 0.12, lo + (hi - lo) * 0.07, "parity", color=TEXT2, fontsize=8, rotation=45)
    ax.text(lo + (hi - lo) * 0.04, hi - (hi - lo) * 0.06, region_text, color=TEXT2, fontsize=8.5, va="top")
    for policy, label, color in POLICIES:
        x, x_error = stats[("ai_decide_split", policy)][value], stats[("ai_decide_split", policy)][error]
        y, y_error = stats[("service_policies", policy)][value], stats[("service_policies", policy)][error]
        ax.errorbar(x, y, xerr=x_error, yerr=y_error, fmt="none", ecolor=color, elinewidth=1.5,
                    capsize=3, zorder=2)
        ax.scatter([x], [y], s=70, color=color, edgecolors=SURFACE, linewidths=2, zorder=3, label=label)
        side, pad = SIDES[kind][policy], (hi - lo) * 0.015
        position = {
            "right": (x + x_error + pad, y, "left", "center"),
            "left": (x - x_error - pad, y, "right", "center"),
            "above": (x, y + y_error + pad, "center", "bottom"),
            "below": (x, y - y_error - pad, "center", "top"),
        }[side]
        ax.text(position[0], position[1], label, ha=position[2], va=position[3], color=TEXT, fontsize=8.5)
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_aspect("equal")
    ax.set_xlabel(f"ai_decide (split) {unit}", color=TEXT2, fontsize=9)
    ax.set_ylabel(f"Service policies {unit}", color=TEXT2, fontsize=9)
    ax.set_title(title, color=TEXT, fontsize=10.5, loc="left", pad=10)
    ax.grid(color=GRID, lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT2, labelsize=8, length=0)
    ax.legend(loc=legend_loc, bbox_to_anchor=(0.02, 0.9) if legend_loc == "upper left" else None,
              frameon=False, fontsize=8, labelcolor=TEXT2, handletextpad=0.3)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def flow_diagram(path):
    fig, ax = plt.subplots(figsize=(11, 3.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 3.6)
    ax.axis("off")

    def box(x, y, width, height, label, edge=LINE, rounding=0.08, weight="normal"):
        ax.add_patch(FancyBboxPatch((x - width / 2, y - height / 2), width, height,
                                    boxstyle=f"round,pad=0,rounding_size={rounding}",
                                    facecolor=SURFACE, edgecolor=edge, linewidth=1.5, zorder=2))
        ax.text(x, y, label, ha="center", va="center", fontsize=9.5, color=TEXT, zorder=3, weight=weight)

    def arrow(start, end, label=None, label_xy=None, dashed=False, color=LINE):
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=11, linewidth=1.4,
                                     color=color, linestyle=(0, (3, 2)) if dashed else "-",
                                     shrinkA=0, shrinkB=0, zorder=1))
        if label:
            ax.text(*label_xy, label, ha="center", va="bottom", fontsize=8.5, color=TEXT2)

    blue, yellow = POLICIES[0][2], POLICIES[3][2]
    ax.add_patch(FancyBboxPatch((2.05, 0.75), 2.3, 2.55, boxstyle="round,pad=0,rounding_size=0.1",
                                facecolor="#eef4fc", edgecolor=blue, linewidth=1.2, zorder=0))
    ax.text(3.2, 3.12, "Input guardrails · in parallel", ha="center", va="center", fontsize=9, color=TEXT2)
    box(0.85, 1.85, 1.3, 0.62, "User prompt", rounding=0.31)
    for label, y in {"Jailbreak": 2.55, "Unsafe content": 1.85, "Sensitive data": 1.15}.items():
        box(3.2, y, 1.8, 0.5, label, edge=blue)
        arrow((1.5, 1.85), (2.3, y))
    box(5.65, 1.85, 1.6, 0.8, "LLM call\nGemini 3.6 Flash", weight="bold")
    arrow((4.35, 1.85), (4.85, 1.85), "all allow", (4.66, 2.0))
    box(7.85, 1.85, 1.7, 0.8, "Output guardrail\nHallucination", edge=yellow)
    arrow((6.45, 1.85), (7.0, 1.85))
    box(10.1, 2.55, 1.3, 0.55, "Response", rounding=0.27)
    arrow((8.7, 2.05), (9.45, 2.5), "allow", (9.0, 2.35))
    box(10.1, 0.75, 1.3, 0.55, "Blocked", edge=BLOCK, rounding=0.27)
    arrow((8.7, 1.6), (9.45, 0.85), "flags", (9.25, 1.25), dashed=True, color=BLOCK)
    arrow((3.2, 0.75), (9.45, 0.66), "any input flags → stop, no LLM call", (6.2, 0.38),
          dashed=True, color=BLOCK)
    fig.tight_layout(pad=0.3)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def render_pdf(markdown_path, pdf_path):
    body = markdown.markdown(markdown_path.read_text(encoding="utf-8"), extensions=["tables"])
    html = f"<html><head><meta charset='utf-8'><style>{PDF_CSS}</style></head><body>{body}</body></html>"
    # The HTML sits beside the images so relative image paths resolve.
    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=ANALYSIS_DIR, delete=False,
                                     encoding="utf-8") as html_file:
        html_file.write(html)
    html_path = Path(html_file.name)
    try:
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={pdf_path}", html_path.as_uri()],
            check=True, capture_output=True,
        )
    finally:
        html_path.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_csv", type=Path)
    arguments = parser.parse_args()
    stats = policy_stats(arguments.results_csv)
    scatter(stats, "latency", 0, 9, "end-to-end latency (s)",
            "End-to-end latency per policy's prompts (mean ± SE)",
            "Above the line: service policies slower", "lower right", ANALYSIS_DIR / "readout_latency.png")
    scatter(stats, "accuracy", 60, 100, "accuracy (%)", "Accuracy per policy (mean ± SE)",
            "Above the line: service policies more accurate", "upper left",
            ANALYSIS_DIR / "readout_accuracy.png")
    flow_diagram(ANALYSIS_DIR / "readout_flow.png")
    render_pdf(ANALYSIS_DIR / "readout.md", ANALYSIS_DIR / "readout.pdf")
    for policy, label, _ in POLICIES:
        service, decide = stats[("service_policies", policy)], stats[("ai_decide_split", policy)]
        print(f"{label}: service policies {service[0]:.2f}±{service[1]:.2f} s, {service[2]:.1f}±{service[3]:.1f}% "
              f"| ai_decide {decide[0]:.2f}±{decide[1]:.2f} s, {decide[2]:.1f}±{decide[3]:.1f}%")
    print(f"Wrote charts and {ANALYSIS_DIR / 'readout.pdf'}")


if __name__ == "__main__":
    main()
