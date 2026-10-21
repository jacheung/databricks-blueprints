#!/usr/bin/env python3
"""Build readouts/<model>/ (README.md + charts) from a benchmark run's saved Unity Catalog data.

No endpoints needed: reads serving_bench_summary and serving_bench_requests for one run_id.

    python make_readout.py --config configs/gte-large-en-v1.5.yml --run-id run_x --profile PROFILE
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import (  # noqa: E402
    MAX_ERROR_RATE,
    endpoint_label,
    error_rate,
    load_config,
    readout,
    readout_document,
)

CHARTS = ("throughput.png", "latency_percentiles.png", "latency_distribution.png", "idle.png")

# Reference categorical palette (dataviz skill), fixed order; validated for adjacent pairs.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
# Color follows the serving option across every readout (T4 is always the same hue), not list position.
FIXED_SLOTS = ["PT", "Custom GPU (T4)", "Custom GPU (A10G)", "Pay-per-token",
               "Custom GPU (L40)", "Custom GPU (A100)", "Custom GPU (L4)"]
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

INTS = {"concurrency", "idle_seconds", "requests", "errors", "rate_limited", "status", "rep", "successes"}
FLOATS = {"latency_ms", "rps", "p50", "p95", "p99", "duration_s"}


def normalize(row: dict) -> dict:
    """CLI query results are all strings; empty means null."""
    out = {}
    for key, value in row.items():
        if value in ("", None):
            out[key] = None
        elif key in INTS:
            out[key] = int(value)
        elif key in FLOATS:
            out[key] = float(value)
        elif key == "success":
            out[key] = value in (True, "true")
        else:
            out[key] = value
    return out


def fetch_run(run_id: str, catalog: str, schema: str, query) -> tuple[list[dict], list[dict], str]:
    """query: callable(sql) -> list of row dicts (CLI locally, spark.sql(...) in a notebook)."""
    t = f"`{catalog}`.`{schema}`"
    summary = [normalize(r) for r in query(
        f"SELECT endpoint, concurrency, requests, rps, p50, p95, p99, errors, duration_s, "
        f"cast(started_at AS DATE) day "
        f"FROM {t}.serving_bench_summary WHERE run_id = '{run_id}'")]
    if not summary:
        raise SystemExit(f"No summary rows for {run_id} in {t}.serving_bench_summary")
    requests = [normalize(r) for r in query(
        f"SELECT endpoint, phase, concurrency, idle_seconds, latency_ms, success, status "
        f"FROM {t}.serving_bench_requests WHERE run_id = '{run_id}' AND phase IN ('load', 'idle_probe')")]
    # Counted from requests so runs saved before the summary had a rate_limited column work too.
    for level in summary:
        level["rate_limited"] = sum(
            r["phase"] == "load" and r["status"] == 429 and r["endpoint"] == level["endpoint"]
            and r["concurrency"] == level["concurrency"] for r in requests)
    return summary, requests, str(summary[0]["day"])


def cli_query(profile: str):
    def run(sql: str) -> list[dict]:
        out = subprocess.run(
            ["databricks", "experimental", "aitools", "tools", "query", "--profile", profile, sql, "-o", "json"],
            capture_output=True, text=True,
        )
        if out.returncode:
            raise SystemExit(f"Query failed: {out.stderr.strip()[-500:]}")
        data = json.loads(out.stdout)
        return data if isinstance(data, list) else data.get("rows", [])

    return run


# ── Charts ────────────────────────────────────────────────────────────


def _style(ax, title: str | None = None):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, color=INK, fontsize=11, loc="left")


def _figure(ncols: int, width: float = 4.6, height: float = 3.6):
    fig, axes = plt.subplots(1, ncols, figsize=(width * ncols, height), squeeze=False, facecolor=SURFACE)
    return fig, list(axes[0])


def _concurrency_axis(ax, levels):
    ax.set_xscale("log", base=2)
    ax.set_xticks(levels, [str(c) for c in levels])
    ax.minorticks_off()
    ax.set_xlabel("Client concurrency", color=INK_2, fontsize=9)


MS_TICKS = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)


def _log_ms_axis(ax, low, high):
    """Log scale with plain millisecond labels instead of 10^n."""
    ax.set_yscale("log")
    ax.set_ylim(low, high)
    ticks = [t for t in MS_TICKS if low <= t <= high]
    ax.set_yticks(ticks, [f"{t:,}" for t in ticks])
    ax.minorticks_off()


def _legend(fig, endpoints, colors):
    handles = [plt.Line2D([], [], color=colors[e], linewidth=2, marker="o", markersize=6) for e in endpoints]
    fig.legend(handles, endpoints, loc="lower center", ncol=len(endpoints), frameon=False,
               fontsize=9, labelcolor=INK)


def _save(fig, path: Path, bottom: float = 0.22):
    fig.tight_layout(rect=(0, bottom - 0.08, 1, 1))
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_throughput(summary, endpoints, colors, path: Path):
    fig, (ax,) = _figure(1, width=7, height=4.2)
    _style(ax, "Achieved throughput by concurrency")
    levels = sorted({r["concurrency"] for r in summary})
    for e in endpoints:
        rows = sorted((r for r in summary if r["endpoint"] == e), key=lambda r: r["concurrency"])
        ok = [r for r in rows if error_rate(r) <= MAX_ERROR_RATE]
        bad = [r for r in rows if error_rate(r) > MAX_ERROR_RATE]
        ax.plot([r["concurrency"] for r in ok], [r["rps"] for r in ok], color=colors[e], linewidth=2,
                marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
        for r in bad:  # hollow: RPS counts only requests that got through
            ax.plot(r["concurrency"], r["rps"], marker="o", markersize=8, markerfacecolor=SURFACE,
                    markeredgecolor=colors[e], markeredgewidth=2, linestyle="none")
            ax.annotate(f"{error_rate(r):.0%} errors", (r["concurrency"], r["rps"]), xytext=(-6, 8),
                        textcoords="offset points", ha="right", fontsize=8, color=INK_2)
        if ok:
            ax.annotate(e, (ok[-1]["concurrency"], ok[-1]["rps"]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8, color=INK)
    _concurrency_axis(ax, levels)
    ax.set_ylabel("Successful requests / s", color=INK_2, fontsize=9)
    ax.set_ylim(bottom=0)
    ax.set_xlim(levels[0] / 1.3, levels[-1] * 2.2)
    _legend(fig, endpoints, colors)
    _save(fig, path, bottom=0.16)


def plot_latency_percentiles(summary, endpoints, colors, path: Path):
    fig, axes = _figure(3)
    levels = sorted({r["concurrency"] for r in summary})
    for ax, metric in zip(axes, ("p50", "p95", "p99")):
        _style(ax, f"{metric.upper()} latency")
        for e in endpoints:
            rows = sorted((r for r in summary if r["endpoint"] == e and r[metric] is not None),
                          key=lambda r: r["concurrency"])
            ok = [r for r in rows if error_rate(r) <= MAX_ERROR_RATE]
            ax.plot([r["concurrency"] for r in ok], [r[metric] for r in ok], color=colors[e],
                    linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE, markeredgewidth=1.2)
            for r in rows:
                if error_rate(r) > MAX_ERROR_RATE:  # hollow, as in the throughput chart
                    ax.plot(r["concurrency"], r[metric], marker="o", markersize=7, markerfacecolor=SURFACE,
                            markeredgecolor=colors[e], markeredgewidth=2, linestyle="none")
        _concurrency_axis(ax, levels)
    axes[0].set_ylabel("Latency (ms, log scale)", color=INK_2, fontsize=9)
    values = [r[m] for r in summary for m in ("p50", "p95", "p99") if r[m]]
    for ax in axes:
        _log_ms_axis(ax, min(values) / 1.3, max(values) * 1.3)
    _legend(fig, endpoints, colors)
    _save(fig, path)


def plot_latency_distribution(requests, endpoints, colors, path: Path):
    """Every successful request per level: box = P25–P75, whiskers = P5–P95."""
    fig, axes = _figure(len(endpoints), width=3.6, height=3.6)
    load = [r for r in requests if r["phase"] == "load" and r["success"]]
    # Fit the y-range to what's drawn (P5–P95 whiskers), not to undrawn outliers.
    groups = {}
    for r in load:
        groups.setdefault((r["endpoint"], r["concurrency"]), []).append(r["latency_ms"])
    bounds = [q for ms in groups.values() if len(ms) > 1
              for q in statistics.quantiles(ms, n=20, method="inclusive")[::18]]
    for ax, e in zip(axes, endpoints):
        _style(ax, e)
        levels = sorted({r["concurrency"] for r in load if r["endpoint"] == e})
        data = [[r["latency_ms"] for r in load if r["endpoint"] == e and r["concurrency"] == c] for c in levels]
        box = ax.boxplot(data, positions=range(len(levels)), widths=0.55, whis=(5, 95), showfliers=False,
                         patch_artist=True, medianprops={"color": INK, "linewidth": 1.5})
        for patch in box["boxes"]:
            patch.set(facecolor=colors[e], alpha=0.35, edgecolor=colors[e], linewidth=1.2)
        for part in ("whiskers", "caps"):
            for line in box[part]:
                line.set(color=colors[e], linewidth=1.2)
        ax.set_xticks(range(len(levels)), [str(c) for c in levels])
        ax.set_xlabel("Client concurrency", color=INK_2, fontsize=9)
        _log_ms_axis(ax, min(bounds) / 1.3, max(bounds) * 1.3)
    axes[0].set_ylabel("Latency (ms, log scale)", color=INK_2, fontsize=9)
    _save(fig, path, bottom=0.08)


def _idle_label(seconds: int) -> str:
    if seconds == 0:
        return "warm"
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


def plot_idle(requests, endpoints, colors, path: Path):
    fig, axes = _figure(2, width=5.2)
    probes = [r for r in requests if r["phase"] == "idle_probe"]
    durations = sorted({r["idle_seconds"] for r in probes})
    for ax, (title, stat) in zip(axes, (("Mean latency after idle", statistics.mean),
                                        ("Std deviation after idle", statistics.stdev))):
        _style(ax, title)
        for e in endpoints:
            xs, ys = [], []
            for i, d in enumerate(durations):
                ms = [r["latency_ms"] for r in probes if r["endpoint"] == e and r["idle_seconds"] == d and r["success"]]
                if len(ms) >= (2 if stat is statistics.stdev else 1):
                    xs.append(i)
                    ys.append(stat(ms))
            ax.plot(xs, ys, color=colors[e], linewidth=2, marker="o", markersize=8,
                    markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax.set_xticks(range(len(durations)), [_idle_label(d) for d in durations])
        ax.set_xlabel("Idle before probe", color=INK_2, fontsize=9)
        ax.set_ylabel("ms", color=INK_2, fontsize=9)
        ax.set_ylim(bottom=0)
    _legend(fig, endpoints, colors)
    _save(fig, path)


# ── Markdown ──────────────────────────────────────────────────────────


def level_table(summary: list[dict]) -> str:
    def ms(v):
        return "—" if v is None else f"{v:.0f}"

    lines = ["| Endpoint | Concurrency | Requests | RPS | P50 ms | P95 ms | P99 ms | Errors | Duration s | Note |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for r in summary:
        note = ""
        if error_rate(r) > MAX_ERROR_RATE:
            note = f"excluded: {error_rate(r):.0%} errors" + (" (429)" if r.get("rate_limited") else "")
        lines.append(f"| {r['endpoint']} | {r['concurrency']} | {r['requests']} | {r['rps']:.1f} | {ms(r['p50'])} "
                     f"| {ms(r['p95'])} | {ms(r['p99'])} | {r['errors']} | {r['duration_s']:.1f} | {note} |")
    return "\n".join(lines)


def series_colors(endpoints: list[str]) -> dict[str, str]:
    colors = {e: SERIES[FIXED_SLOTS.index(e)] for e in endpoints if e in FIXED_SLOTS}
    spare = (c for c in SERIES if c not in colors.values())
    return colors | {e: next(spare, SERIES[-1]) for e in endpoints if e not in colors}


def write_readout(out_dir: Path, meta: dict, summary: list[dict], requests: list[dict]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    endpoints = [ep["label"] for ep in meta["endpoints"]]
    endpoints = [e for e in endpoints if any(r["endpoint"] == e for r in summary)]
    colors = series_colors(endpoints)
    summary = sorted(summary, key=lambda r: (endpoints.index(r["endpoint"]), r["concurrency"]))
    probes = [r | {"latency_ms": r["latency_ms"] if r["success"] else None}
              for r in requests if r["phase"] == "idle_probe"]

    plot_throughput(summary, endpoints, colors, out_dir / CHARTS[0])
    plot_latency_percentiles(summary, endpoints, colors, out_dir / CHARTS[1])
    plot_latency_distribution(requests, endpoints, colors, out_dir / CHARTS[2])
    plot_idle(requests, endpoints, colors, out_dir / CHARTS[3])

    doc = readout_document(meta, readout(summary, probes))
    doc += f"""
## Charts

### Throughput

![Achieved throughput by concurrency]({CHARTS[0]})

Successful requests per second at each client concurrency. A flat line means the endpoint is saturated:
more clients only queue. Hollow markers are levels with more than 1% errors; their RPS counts only the
requests that got through.

### Latency percentiles

![P50, P95 and P99 latency by concurrency]({CHARTS[1]})

P50/P95/P99 of successful requests at each level (log scale). Past saturation, latency grows roughly
in proportion to concurrency.

### Latency distribution

![Latency distribution per endpoint and concurrency]({CHARTS[2]})

Every successful load request: box = P25–P75, line = median, whiskers = P5–P95 (log scale).

### Latency after idle

![Mean and standard deviation of latency after idle]({CHARTS[3]})

One probe per rep after each idle period, from a warm endpoint. Client-observed: this can't separate a
cold start from shared-pool load or connection setup.

## Per-level results

{level_table(summary)}
"""
    (out_dir / "README.md").write_text(doc)
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True, help="configs/<model>.yml the run used")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", required=True, help="Databricks CLI profile")
    parser.add_argument("--out", help="output folder (default: readouts/<hf repo name, lowercased>)")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient

    cfg = load_config(args.config)
    uc = cfg["unity_catalog"]
    summary, requests, day = fetch_run(args.run_id, uc["catalog"], uc["schema"], cli_query(args.profile))
    meta = {
        "run_id": args.run_id, "model": cfg["model"]["hf_name"], "task": cfg["model"]["task"],
        "input_tokens": cfg["load_test"]["input_tokens"], "date": day, "ai_env": cfg["model"].get("ai_env"),
        "workspace": WorkspaceClient(profile=args.profile).config.host.rstrip("/"),
        "load_test": cfg["load_test"], "idle_test": cfg.get("idle_test") or {},
        "endpoints": [ep | {"label": endpoint_label(ep)} for ep in cfg["endpoints"]],
    }
    out = Path(args.out or Path(__file__).parent / "readouts" / cfg["model"]["hf_name"].split("/")[-1].lower())
    print(write_readout(out, meta, summary, requests) / "README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
