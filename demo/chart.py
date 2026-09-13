"""Render a load-sweep JSON into a self-contained HTML chart.

Reads either output shape -- ``python -m loadtest --json ...`` (real HTTP) or
``python -m demo.measure_inprocess --json ...`` (no server needed). Both carry a
``phases`` list, one entry per offered-rate level.

    python -m demo.chart demo/sample/sweep.json -o demo/sample/chart.html

No third-party dependencies and no network: the output is one file with the SVG
inlined, so it opens offline and screen-records cleanly.

Three panels rather than one chart with two y-axes. Rates (rps) and latency (ms)
do not share a scale, and overlaying them on twin axes lets the author imply any
correlation they like by sliding one scale against the other.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Validated against the dataviz six-checks in both modes (worst adjacent CVD
# delta-E 24.7 light / 26.8 dark, against the >=8 target).
LIGHT = {
    "surface": "#fcfcfb", "plane": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
    "goodput": "#2a78d6", "shed": "#eb6834", "p50": "#86b6ef", "p95": "#1c5cab",
    "border": "rgba(11,11,11,0.10)",
}
DARK = {
    "surface": "#1a1a19", "plane": "#0d0d0d", "ink": "#ffffff", "ink2": "#c3c2b7",
    "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
    "goodput": "#3987e5", "shed": "#d95926", "p50": "#6da7ec", "p95": "#9ec5f4",
    "border": "rgba(255,255,255,0.10)",
}

W, H = 720, 300
PAD = {"t": 28, "r": 92, "b": 44, "l": 64}


def nice_max(value: float) -> float:
    """Smallest readable 4-step axis maximum that still contains `value`.

    Scanning multipliers in order and returning the first fit picks whichever
    candidate happens to come first, not the tightest one -- which left a 15,000
    series sitting under a 40,000 axis. Take the minimum of all candidates.
    """
    if value <= 0:
        return 1.0
    candidates = [
        mult * (10 ** exp) * 4
        for mult in (1, 1.5, 2, 2.5, 3, 4, 5)
        for exp in range(-3, 7)
    ]
    fits = [c for c in candidates if c >= value]
    return min(fits) if fits else value


def scale_x(v: float, lo: float, hi: float) -> float:
    span = hi - lo or 1
    return PAD["l"] + (v - lo) / span * (W - PAD["l"] - PAD["r"])


def scale_y(v: float, top: float) -> float:
    return H - PAD["b"] - (v / (top or 1)) * (H - PAD["t"] - PAD["b"])


def path(points: list[tuple[float, float]]) -> str:
    return " ".join(("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}" for i, (x, y) in enumerate(points))


def panel(
    title: str,
    subtitle: str,
    series: list[dict],
    xs: list[float],
    y_label: str,
    fmt: str = "{:.1f}",
) -> str:
    """One chart panel. `series` entries: {name, key(role), values, dashed?}"""
    lo, hi = min(xs), max(xs)
    top = nice_max(max((max(s["values"]) for s in series if s["values"]), default=1))

    grid, ticks = [], []
    for i in range(5):
        v = top * i / 4
        y = scale_y(v, top)
        grid.append(f'<line x1="{PAD["l"]}" y1="{y:.1f}" x2="{W - PAD["r"]}" y2="{y:.1f}" class="grid"/>')
        ticks.append(
            f'<text x="{PAD["l"] - 10}" y="{y + 4:.1f}" class="tick" text-anchor="end">{fmt.format(v)}</text>'
        )
    for x in xs:
        px = scale_x(x, lo, hi)
        ticks.append(
            f'<text x="{px:.1f}" y="{H - PAD["b"] + 20}" class="tick" text-anchor="middle">{x:g}</text>'
        )

    marks, anchors = [], []
    for s in series:
        pts = [(scale_x(x, lo, hi), scale_y(v, top)) for x, v in zip(xs, s["values"])]
        dash = ' stroke-dasharray="5 4"' if s.get("dashed") else ""
        marks.append(f'<path d="{path(pts)}" fill="none" class="line {s["key"]}"{dash}/>')
        for (px, py) in pts:
            # 2px surface ring keeps overlapping markers readable
            marks.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="dot {s["key"]}"/>')
        lx, ly = pts[-1]
        anchors.append([ly, lx, s])

    # Converging series put their end labels on top of each other -- p95 and p50
    # land within a pixel of one another once latency plateaus. Walk them in
    # vertical order and push each down until it clears the previous one.
    MIN_GAP = 13.0
    anchors.sort(key=lambda a: a[0])
    for i in range(1, len(anchors)):
        if anchors[i][0] - anchors[i - 1][0] < MIN_GAP:
            anchors[i][0] = anchors[i - 1][0] + MIN_GAP
    labels = [
        f'<text x="{lx + 10:.1f}" y="{ly + 4:.1f}" class="dlabel {s["key"]}">{s["name"]}</text>'
        for ly, lx, s in anchors
    ]

    hover = []
    for i, x in enumerate(xs):
        px = scale_x(x, lo, hi)
        rows = "&#10;".join(f'{s["name"]}: {fmt.format(s["values"][i])}' for s in series)
        hover.append(
            f'<rect x="{px - 14:.1f}" y="{PAD["t"]}" width="28" '
            f'height="{H - PAD["t"] - PAD["b"]}" class="hit"><title>{x:g} rps offered&#10;{rows}</title></rect>'
        )

    return f"""<figure class="panel">
  <figcaption><h3>{title}</h3><p>{subtitle}</p></figcaption>
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="{title}. {subtitle}">
    <text x="{PAD['l'] - 10}" y="{PAD['t'] - 12}" class="axis-name" text-anchor="end">{y_label}</text>
    {''.join(grid)}
    <line x1="{PAD['l']}" y1="{H - PAD['b']}" x2="{W - PAD['r']}" y2="{H - PAD['b']}" class="baseline"/>
    {''.join(ticks)}
    {''.join(marks)}
    {''.join(labels)}
    {''.join(hover)}
    <text x="{(PAD['l'] + W - PAD['r']) / 2:.0f}" y="{H - 6}" class="axis-name" text-anchor="middle">offered load (requests/sec)</text>
  </svg>
</figure>"""


def build(data: dict) -> str:
    phases = data["phases"]
    cfg = data.get("config", {})
    xs = [float(p.get("rps_target") or p.get("concurrency") or i) for i, p in enumerate(phases)]

    # Offered counts ARRIVALS in the window, not resolutions. In the transition
    # region some arrivals are still queued when the window closes; counting only
    # what resolved would understate the load the server was actually handed.
    offered = [
        (p.get("arrivals_in_window", p["issued"])) / p["wall_s"] if p["wall_s"] else 0
        for p in phases
    ]
    goodput = [p["goodput_rps"] for p in phases]
    shed = [p["rejected"] / p["wall_s"] if p["wall_s"] else 0 for p in phases]
    ttft50 = [p["ttft"].get("p50", 0) * 1000 for p in phases]
    ttft95 = [p["ttft"].get("p95", 0) * 1000 for p in phases]
    itl50 = [p["itl"].get("p50", 0) * 1000 for p in phases]

    failed = sum(p["failed"] for p in phases)
    flat_lo, flat_hi = min(goodput[len(goodput) // 2:]), max(goodput)
    shed_pct = [100 * p["rejected"] / p["issued"] if p["issued"] else 0 for p in phases]

    rates = panel(
        "Goodput holds flat while the excess is shed",
        f"Offered load rises {xs[0]:g}&rarr;{xs[-1]:g} rps. Successful throughput does not move.",
        [
            {"name": "offered", "key": "s-off", "values": offered, "dashed": True},
            {"name": "goodput", "key": "s-good", "values": goodput},
            {"name": "shed", "key": "s-shed", "values": shed},
        ],
        xs, "req/s", "{:.0f}",
    )
    latency = panel(
        "Queue wait is what degrades, and it is bounded",
        "Time to first token, measured from arrival &mdash; so queue wait is inside it.",
        [
            {"name": "p95", "key": "s-p95", "values": ttft95},
            {"name": "p50", "key": "s-p50", "values": ttft50},
        ],
        xs, "ms", "{:.0f}",
    )
    itl = panel(
        "Tokens keep flowing at the same pace for whoever got in",
        "Inter-token latency, p50. Bounded concurrency means the engine is never oversubscribed.",
        [{"name": "p50", "key": "s-good", "values": itl50}],
        xs, "ms", "{:.0f}",
    )

    rows = "".join(
        f"<tr><td>{x:g}</td><td>{o:.1f}</td><td>{g:.2f}</td><td>{s:.1f}</td>"
        f"<td>{sp:.0f}%</td><td>{t5:.0f}</td><td>{t9:.0f}</td><td>{i5:.0f}</td></tr>"
        for x, o, g, s, sp, t5, t9, i5 in zip(xs, offered, goodput, shed, shed_pct, ttft50, ttft95, itl50)
    )

    def tokens(c: dict) -> str:
        """Just the custom-property declarations, so each scope can wrap them itself."""
        return (
            f"--surface:{c['surface']}; --plane:{c['plane']}; --ink:{c['ink']}; "
            f"--ink2:{c['ink2']}; --muted:{c['muted']}; --grid:{c['grid']}; "
            f"--axis:{c['axis']}; --border:{c['border']}; --s-off:{c['muted']}; "
            f"--s-good:{c['goodput']}; --s-shed:{c['shed']}; "
            f"--s-p50:{c['p50']}; --s-p95:{c['p95']};"
        )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Admission control under overload</title>
<style>
:root {{ color-scheme:light; {tokens(LIGHT)} }}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{ color-scheme:dark; {tokens(DARK)} }}
}}
:root[data-theme="dark"] {{ color-scheme:dark; {tokens(DARK)} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--plane); color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:820px; margin:0 auto; padding:32px 20px 56px; }}
h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:-0.01em; }}
.sub {{ color:var(--ink2); margin:0 0 24px; }}
.hero {{ background:var(--surface); border:1px solid var(--border); border-radius:10px;
  padding:18px 20px; margin-bottom:24px; }}
.hero b {{ display:block; font-size:40px; line-height:1.1; letter-spacing:-0.02em; }}
.hero span {{ color:var(--ink2); }}
.panel {{ margin:0 0 26px; background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:16px 12px 8px; overflow-x:auto; }}
figcaption {{ padding:0 8px; }}
h3 {{ font-size:15px; margin:0 0 2px; }}
figcaption p {{ margin:0 0 6px; color:var(--ink2); font-size:13px; }}
svg {{ display:block; width:100%; height:auto; min-width:560px; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.baseline {{ stroke:var(--axis); stroke-width:1; }}
.tick, .axis-name {{ fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }}
.line {{ stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.dot {{ stroke:var(--surface); stroke-width:2; }}
.dlabel {{ font-size:12px; font-weight:600; }}
.hit {{ fill:transparent; }} .hit:hover {{ fill:var(--grid); opacity:.45; }}
.s-off {{ stroke:var(--s-off); }} .dot.s-off {{ fill:var(--s-off); }} text.s-off {{ fill:var(--ink2); stroke:none; }}
.s-good {{ stroke:var(--s-good); }} .dot.s-good {{ fill:var(--s-good); }} text.s-good {{ fill:var(--ink); stroke:none; }}
.s-shed {{ stroke:var(--s-shed); }} .dot.s-shed {{ fill:var(--s-shed); }} text.s-shed {{ fill:var(--ink); stroke:none; }}
.s-p50 {{ stroke:var(--s-p50); }} .dot.s-p50 {{ fill:var(--s-p50); }} text.s-p50 {{ fill:var(--ink); stroke:none; }}
.s-p95 {{ stroke:var(--s-p95); }} .dot.s-p95 {{ fill:var(--s-p95); }} text.s-p95 {{ fill:var(--ink); stroke:none; }}
.key {{ display:flex; gap:16px; flex-wrap:wrap; padding:0 8px 10px; color:var(--ink2); font-size:12px; }}
.key i {{ display:inline-block; width:14px; height:2px; vertical-align:middle; margin-right:6px; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; font-variant-numeric:tabular-nums; }}
caption {{ text-align:left; font-weight:600; padding:0 0 8px; font-size:13px; }}
th,td {{ text-align:right; padding:5px 8px; border-bottom:1px solid var(--grid); }}
th:first-child,td:first-child {{ text-align:left; }}
th {{ color:var(--ink2); font-weight:600; }}
footer {{ color:var(--muted); font-size:12px; margin-top:22px; }}
</style>
<div class="wrap">
<h1>Admission control under overload</h1>
<p class="sub">Bounded concurrency ({cfg.get('max_concurrent', '?')} slots) over a bounded FIFO queue
({cfg.get('max_queue_size', '?')} deep), {cfg.get('max_tokens', '?')} tokens per request.</p>

<div class="hero">
  <span>Goodput across a {xs[-1] / xs[0]:.0f}&times; range of offered load</span>
  <b>{flat_lo:.1f}&ndash;{flat_hi:.1f} req/s</b>
  <span>{failed} failed requests. Everything above capacity was refused in milliseconds, not queued indefinitely.</span>
</div>

<div class="key">
  <span><i style="background:var(--s-off)"></i>offered</span>
  <span><i style="background:var(--s-good)"></i>goodput (succeeded)</span>
  <span><i style="background:var(--s-shed)"></i>shed (429/503)</span>
</div>
{rates}
{latency}
{itl}

<table>
  <caption>Measured values</caption>
  <thead><tr><th>offered</th><th>arrived/s</th><th>goodput/s</th><th>shed/s</th>
  <th>shed %</th><th>TTFT p50 ms</th><th>TTFT p95 ms</th><th>ITL p50 ms</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<footer>Source: {cfg.get('source', 'HTTP load harness')}. Seed {cfg.get('seed', '?')},
{cfg.get('duration_s', '?')}s per level.</footer>
</div>
"""


def main() -> None:
    ap = argparse.ArgumentParser(prog="demo.chart", description=__doc__)
    ap.add_argument("input", help="sweep JSON from loadtest or demo.measure_inprocess")
    ap.add_argument("-o", "--output", default="demo/sample/chart.html")
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text())
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(data), encoding="utf-8")
    print(f"wrote {out}  ({len(data['phases'])} levels)")


if __name__ == "__main__":
    main()
