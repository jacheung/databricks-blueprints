"""Prototype vs production for many local models: training (top row) and inference (bottom row).

  uv run --no-project --with fonttools --with brotli python docs/make_scaling.py [--theme databricks|jacheung] [--png]

Left column: sequential single-node fits, then one endpoint per model. Right column: the same fit run in parallel
by Spark or Ray, then one router endpoint that fetches each group's weights per request.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "diagramsmith" / "scripts"))
from diagramsmith import HEAD, Diagram, args, node_height  # noqa: E402

DELTA, SERVING = "primary-icon-orange-delta-table.svg", "primary-icon-orange-productionize.svg"
SPARK, RAY = ("apache-spark-logo.svg", 414 / 215), ("ray-logo.svg", 328 / 129)

W, H = 1260, 684
LX, RX, CW = 20, 640, 600  # left and right column x, column width
R1, R1H = 124, 248  # training row: panel y, height
R2, R2H = 388, 278  # inference row
SEG_W, SEG_GAP, LANE_H = 58, 6, 36


def segments(d, x, y, h, names):
    """Fit boxes in a lane, left to right."""
    for name in names:
        w = SEG_W if len(name) < 8 else 80
        d.box(x, y + 5, w, h - 10, rx=5)
        d.sub(x + w / 2, y + h / 2 + 4, name, "text")
        x += w + SEG_GAP


def panel(d, x, y, w, h, name, subtitle, production):
    """Prototype panels: dashed, grey title, no fill. Production panels: solid, accent title, warm tint."""
    if production:
        d.box(x, y, w, h, fill="tint_warm_fill", stroke="tint_warm_stroke", sw=1.5, rx=14)
    else:
        d.box(x, y, w, h, fill="panel_fill", stroke="line", rx=14, dash=True)
    d.title(x + 20, y + 30, name, "accent" if production else "muted", "start")
    d.sub(x + 20, y + 49, subtitle, anchor="start")


def build(theme):
    d = Diagram(W, H, theme, flows={"rows": "training data", "request": "request", "response": "response"})
    d.header("Prototype to production: train in parallel, serve behind one router")
    d.legend()

    # column headings and divider
    d.title(LX + 5, 90, "PROTOTYPE", "muted", "start", size=18)
    d.sub(LX + 5, 110, "sequential fits, an endpoint per model: unmanageable at 100k", anchor="start")
    d.title(RX + 5, 90, "PRODUCTION", "accent", "start", size=18)
    d.sub(RX + 5, 110, "parallel fits, one router endpoint for every model", anchor="start")
    d.box((LX + CW + RX) / 2 - 1, 74, 2, H - 84, fill="panel_stroke", stroke="panel_stroke", rx=0)

    # --- train in parallel (production); drawn first because it sets the row's middle --------------------------
    panel(d, RX, R1, CW, R1H, "TRAIN IN PARALLEL", "Spark or Ray runs the same fit on every worker", True)
    cw, ch = 330, 172  # the cluster wraps the workers
    cx, cy = RX + CW - 15 - cw, R1 + 62
    d.box(cx, cy, cw, ch, rx=10)
    sw = SPARK[1] * 26
    d.img(SPARK[0], cx + 14, cy + 7, 26, SPARK[1])
    d.img(RAY[0], cx + 14 + sw + 12, cy + 10, 20, RAY[1])
    d.sub(cx + cw - 14, cy + 23, "cluster", anchor="end")

    lanes = [cy + 40 + (LANE_H + 8) * i for i in range(3)]
    mid = (lanes[0] + lanes[-1] + LANE_H) / 2
    src = d.node(DELTA, "timeseries", RX + 15, mid - node_height(1) / 2, 150, "100k store × SKU")
    bus, lane_x, lane_end = cx + 20, cx + 40, cx + cw - 14
    for i, y in enumerate(lanes):
        d.box(lane_x, y, lane_end - lane_x, LANE_H, fill="tint_cool_fill", stroke="panel_stroke")
        d.sub(lane_x + 10, y + LANE_H / 2 + 4, f"worker {'N' if i == 2 else i + 1}", "text", anchor="start")
        segments(d, lane_x + 72, y, LANE_H, [f"fit g{i + 1 + 3 * k}" for k in range(3)])
        d.arrow(f"M{src[4]} {mid} H{bus} V{y + LANE_H / 2} H{lane_x - HEAD}", "rows")

    # --- train sequentially (prototype) ----------------------------------------------------------------------
    panel(d, LX, R1, CW, R1H, "TRAIN SEQUENTIALLY", "one process fits one group at a time", False)
    src = d.node(DELTA, "timeseries", LX + 15, mid - node_height(1) / 2, 150, "100k store × SKU")
    lx, lend, ly = LX + 200, LX + CW - 15, mid - 22
    d.box(lx, ly, lend - lx, 44, fill="tint_cool_fill", stroke="panel_stroke")
    segments(d, lx + 10, ly + 4, LANE_H, ["fit g1", "fit g2", "fit g3"])
    d.sub((lx + 10 + 3 * (SEG_W + SEG_GAP) + lend - 90) / 2, mid + 4, "· · ·", "text")
    segments(d, lend - 90, ly + 4, LANE_H, ["fit g100k"])
    d.sub(lx, ly - 8, "1 Python process · time →", anchor="start")
    d.arrow(f"M{src[4]} {mid} H{lx - HEAD}", "rows")

    # --- one endpoint per model (prototype) ------------------------------------------------------------------
    panel(d, LX, R2, CW, R2H, "ONE ENDPOINT PER MODEL", "weights packaged inside each endpoint", False)
    ep_w, ep_h, gap = 230, node_height(0), 24
    ep_x = LX + CW - 15 - ep_w
    ys = [R2 + 62 + (ep_h + gap) * i for i in range(3)]
    d.node(SERVING, "endpoint g1", ep_x, ys[0], ep_w)
    g42 = d.node(SERVING, "endpoint g42", ep_x, ys[1], ep_w, accent=True)
    d.node(SERVING, "endpoint g100k", ep_x, ys[2], ep_w)
    for y in ys[:2]:  # gaps between the three endpoints
        d.sub(ep_x + ep_w / 2, y + ep_h + 16, "· · ·", "text")
    mid = g42[2]
    app = d.node(None, "App or job", LX + 15, mid - node_height(1) / 2, 170, "asks for group 42")
    d.arrow(f"M{app[4]} {mid - 8} H{ep_x - HEAD}", "request")
    d.label((app[4] + ep_x) / 2, mid - 18, "g42, horizon 14")
    d.arrow(f"M{ep_x} {mid + 8} H{app[4] + HEAD}", "response")
    d.label((app[4] + ep_x) / 2, mid + 26, "g42's forecast")

    # --- one router endpoint (production) --------------------------------------------------------------------
    panel(d, RX, R2, CW, R2H, "ONE ROUTER ENDPOINT", "weights fetched per request", True)
    rw_ = 270
    rx_ = RX + CW - 15 - rw_
    r = d.node(SERVING, "Router endpoint", rx_, R2 + 58, rw_, "serves all 100k groups",
               "loads g42, then forecasts")
    mid = r[2]
    app = d.node(None, "App or job", RX + 15, mid - node_height(1) / 2, 170, "asks for group 42")
    d.arrow(f"M{app[4]} {mid - 8} H{rx_ - HEAD}", "request")
    d.label((app[4] + rx_) / 2, mid - 18, "g42, horizon 14")
    d.arrow(f"M{rx_} {mid + 8} H{app[4] + HEAD}", "response")
    d.label((app[4] + rx_) / 2, mid + 26, "g42's forecast")

    # model weights: one small artifact per group, g42's highlighted under the fetch arrows
    wy, wh = r[1] + 36, 88
    d.box(rx_, wy, rw_, wh)
    d.title(rx_ + rw_ / 2, wy + 25, "Model weights · one per group")
    cols, rows, cell_w, cell_h, gap = 7, 2, 30, 18, 5
    gx = rx_ + (rw_ - (cols * cell_w + (cols - 1) * gap)) / 2
    for row in range(rows):
        for col in range(cols):
            x, y = gx + col * (cell_w + gap), wy + 37 + row * (cell_h + gap)
            if (row, col) == (0, cols // 2):
                d.box(x, y, cell_w, cell_h, fill="tint_warm_fill", stroke="accent", sw=2, rx=3)
                d.sub(x + cell_w / 2, y + 13, "g42", "text")
            else:
                d.box(x, y, cell_w, cell_h, stroke="panel_stroke", rx=3)
    cx = rx_ + rw_ / 2
    d.varrow(cx - 10, r[1], wy, "request")
    d.varrow(cx + 10, wy, r[1], "response")
    d.label(cx - 20, (r[1] + wy) / 2 + 4, "fetch g42", anchor="end")
    d.label(cx + 20, (r[1] + wy) / 2 + 4, "g42's weights", anchor="start")
    return d


if __name__ == "__main__":
    opt = args(Path(__file__).resolve().parent / "scaling.svg")
    build(opt.theme).save(opt.out, png=opt.png)
