"""Build demo-brief.pdf: the shot list, the measured figures, and the findings.

Renders print-ready HTML, then prints it with Chromium via Playwright:

    python demo/make_brief.py && <print /tmp/brief.html to PDF>

Needs playwright only for the PDF step; the HTML it writes is standalone.

Reuses demo.chart's panel renderer so the figures in the document are the same
vectors as the interactive chart, not screenshots of it.
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo.chart import panel  # noqa: E402

data = json.loads((ROOT / "demo/sample/sweep.json").read_text())
phases = data["phases"]
cfg = data["config"]

xs = [float(p["rps_target"]) for p in phases]
offered = [p.get("arrivals_in_window", p["issued"]) / p["wall_s"] for p in phases]
goodput = [p["goodput_rps"] for p in phases]
shed = [p["rejected"] / p["wall_s"] for p in phases]
ttft50 = [p["ttft"].get("p50", 0) * 1000 for p in phases]
ttft95 = [p["ttft"].get("p95", 0) * 1000 for p in phases]
itl50 = [p["itl"].get("p50", 0) * 1000 for p in phases]
shed_pct = [100 * p["rejected"] / p["issued"] if p["issued"] else 0 for p in phases]
failed = sum(p["failed"] for p in phases)

svg_b64 = base64.b64encode((ROOT / "demo/architecture.svg").read_bytes()).decode()

rates_fig = panel(
    "Goodput holds flat while the excess is shed",
    "Offered load rises 2&rarr;32 req/s. Successful throughput does not move.",
    [
        {"name": "offered", "key": "s-off", "values": offered, "dashed": True},
        {"name": "goodput", "key": "s-good", "values": goodput},
        {"name": "shed", "key": "s-shed", "values": shed},
    ],
    xs, "req/s", "{:.0f}",
)
lat_fig = panel(
    "Queue wait is what degrades &mdash; and it is bounded",
    "Time to first token, measured from arrival, so queue wait is inside it.",
    [
        {"name": "p95", "key": "s-p95", "values": ttft95},
        {"name": "p50", "key": "s-p50", "values": ttft50},
    ],
    xs, "ms", "{:.0f}",
)
itl_fig = panel(
    "Tokens keep flowing at the same pace for whoever got in",
    "Inter-token latency, p50. Bounded concurrency means the engine is never oversubscribed.",
    [{"name": "p50", "key": "s-good", "values": itl50}],
    xs, "ms", "{:.0f}",
)

rows = "".join(
    f"<tr><td>{x:g}</td><td>{o:.1f}</td><td>{g:.2f}</td><td>{s:.1f}</td>"
    f"<td>{sp:.0f}%</td><td>{t5:.0f}</td><td>{t9:.0f}</td><td>{i:.0f}</td></tr>"
    for x, o, g, s, sp, t5, t9, i in zip(
        xs, offered, goodput, shed, shed_pct, ttft50, ttft95, itl50
    )
)

SHOTS = [
    ("0:00&ndash;0:15", "Architecture diagram",
     "A request arrives. Before anything expensive happens, admission control decides whether the "
     "server can take it. Free slot &rarr; the engine, and tokens stream back. All eight busy &rarr; a "
     "short wait in a bounded queue. Queue full &rarr; refused immediately, in about two milliseconds."),
    ("0:15&ndash;0:30", "One request, streaming",
     "Normal case first. The response streams token by token &mdash; the first token arrives long "
     "before the last one is generated."),
    ("0:30&ndash;1:00", "Ramp past capacity (live)",
     "Traffic ramps 4 &rarr; 8 &rarr; 16 &rarr; 32 req/s against the real server, one row per second. "
     "At the first orange row the queue is full and excess is refused. The successful requests keep "
     "completing at the same rate. Nothing times out."),
    ("1:00&ndash;1:20", "The chart",
     "Sixteen times the offered load. Goodput is the flat line; everything above it is shed. Zero "
     "failures. Inter-token latency is flat &mdash; whoever got in gets the same experience at 32 req/s "
     "as at two."),
    ("1:20&ndash;1:30", "Takeaway",
     "Two bounds, two jobs. Concurrency sets throughput. The queue sets how long someone waits before "
     "hearing &ldquo;no&rdquo;. Without them that orange line is a backlog instead &mdash; work piling "
     "up for clients who already gave up."),
]

shot_rows = "".join(
    f'<tr><td class="t">{t}</td><td class="w">{w}</td><td>{s}</td></tr>' for t, w, s in SHOTS
)

HTML = f"""<!doctype html><meta charset="utf-8"><title>Demo brief</title>
<style>
@page {{ size: A4; margin: 15mm 14mm 14mm; }}
:root {{
  --ink:#14130f; --ink2:#4a4842; --muted:#7c7a72; --rule:#d8d6cd; --grid:#e6e4dc;
  --surface:#fcfcfb; --accent:#2a78d6; --shed:#c4551f;
  --s-off:#7c7a72; --s-good:#2a78d6; --s-shed:#eb6834; --s-p50:#86b6ef; --s-p95:#1c5cab;
  --axis:#c3c2b7;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:#fff;
  font:10.5pt/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
h1 {{ font-size:20pt; margin:0 0 2mm; letter-spacing:-0.015em; }}
h2 {{ font-size:12.5pt; margin:0 0 3mm; padding-bottom:1.5mm;
  border-bottom:1px solid var(--rule); letter-spacing:-0.01em; }}
h3 {{ font-size:10.5pt; margin:0 0 1mm; }}
p {{ margin:0 0 3mm; }}
.lede {{ color:var(--ink2); font-size:11pt; margin-bottom:6mm; }}
.meta {{ color:var(--muted); font-size:8.5pt; margin-bottom:8mm; }}
section {{ margin-bottom:8mm; break-inside:avoid; }}
.page {{ break-after:page; }}
table {{ border-collapse:collapse; width:100%; font-size:9pt; }}
th,td {{ text-align:left; padding:2mm 2.5mm; border-bottom:1px solid var(--grid);
  vertical-align:top; }}
th {{ color:var(--ink2); font-weight:650; border-bottom:1px solid var(--rule); }}
td.t {{ white-space:nowrap; font-variant-numeric:tabular-nums; color:var(--ink2); }}
td.w {{ white-space:nowrap; font-weight:600; }}
table.num td, table.num th {{ text-align:right; font-variant-numeric:tabular-nums; }}
table.num td:first-child, table.num th:first-child {{ text-align:left; }}
.hero {{ border:1px solid var(--rule); border-radius:3mm; padding:4mm 5mm; margin-bottom:6mm;
  background:var(--surface); }}
.hero b {{ display:block; font-size:24pt; line-height:1.15; letter-spacing:-0.02em; }}
.hero span {{ color:var(--ink2); font-size:9.5pt; }}
figure.panel {{ margin:0 0 5mm; break-inside:avoid; }}
figcaption p {{ margin:0 0 1mm; color:var(--ink2); font-size:8.5pt; }}
svg {{ display:block; width:100%; height:auto; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.baseline {{ stroke:var(--axis); stroke-width:1; }}
.tick,.axis-name {{ fill:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }}
.line {{ stroke-width:2; fill:none; stroke-linejoin:round; stroke-linecap:round; }}
.dot {{ stroke:#fff; stroke-width:2; }}
.dlabel {{ font-size:12px; font-weight:600; }}
.hit {{ display:none; }}
.s-off{{stroke:var(--s-off)}} .dot.s-off{{fill:var(--s-off)}} text.s-off{{fill:var(--ink2);stroke:none}}
.s-good{{stroke:var(--s-good)}} .dot.s-good{{fill:var(--s-good)}} text.s-good{{fill:var(--ink);stroke:none}}
.s-shed{{stroke:var(--s-shed)}} .dot.s-shed{{fill:var(--s-shed)}} text.s-shed{{fill:var(--ink);stroke:none}}
.s-p50{{stroke:var(--s-p50)}} .dot.s-p50{{fill:var(--s-p50)}} text.s-p50{{fill:var(--ink);stroke:none}}
.s-p95{{stroke:var(--s-p95)}} .dot.s-p95{{fill:var(--s-p95)}} text.s-p95{{fill:var(--ink);stroke:none}}
.key {{ display:flex; gap:6mm; font-size:8.5pt; color:var(--ink2); margin-bottom:3mm; }}
.key i {{ display:inline-block; width:4mm; height:2px; vertical-align:middle; margin-right:1.5mm; }}
.finding {{ border-left:2px solid var(--shed); padding-left:4mm; margin-bottom:6mm;
  break-inside:avoid; }}
.finding h3 {{ color:var(--shed); }}
code {{ font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-size:8.8pt;
  background:#f3f2ee; padding:0.3mm 1mm; border-radius:1mm; }}
.ask {{ background:#f7f6f2; border:1px solid var(--rule); border-radius:2mm; padding:3mm 4mm;
  font-size:9.5pt; }}
img.diagram {{ width:100%; height:auto; }}
footer {{ color:var(--muted); font-size:8pt; border-top:1px solid var(--rule);
  padding-top:2mm; margin-top:6mm; }}
</style>

<h1>Self-Hosted LLM Serving Platform</h1>
<p class="lede">A 90-second demo, and two things the measurements turned up.</p>
<p class="meta">Prepared 13 September 2026 &middot; all figures measured, seed 7,
{cfg.get('duration_s')}s per level ({cfg.get('warmup_s')}s warm-up discarded) &middot;
{cfg.get('max_concurrent')} concurrent slots over a {cfg.get('max_queue_size')}-deep queue</p>

<section>
<h2>1 &nbsp; The 90 seconds</h2>
<table>
<thead><tr><th style="width:20mm">Time</th><th style="width:38mm">Show</th><th>Say</th></tr></thead>
<tbody>{shot_rows}</tbody>
</table>
<p style="margin-top:3mm;font-size:9pt;color:var(--ink2)">
Run <code>.\\demo\\run_demo.ps1</code>. It pauses before each beat, so you can start recording and
get a clean take. The emotional beat is the first orange row in the ramp &mdash; pause there.</p>
</section>

<section class="page">
<h2>2 &nbsp; The picture (0:00&ndash;0:15)</h2>
<img class="diagram" src="data:image/svg+xml;base64,{svg_b64}" alt="Request path through admission control">
</section>

<section>
<h2>3 &nbsp; What was measured (1:00&ndash;1:20)</h2>
<div class="hero">
  <span>Goodput across a 16&times; range of offered load</span>
  <b>4.0&ndash;4.3 req/s</b>
  <span>{failed} failed requests. Everything above capacity was refused in milliseconds
  rather than queued indefinitely.</span>
</div>
<div class="key">
  <span><i style="background:var(--s-off)"></i>offered</span>
  <span><i style="background:var(--s-good)"></i>goodput (succeeded)</span>
  <span><i style="background:var(--s-shed)"></i>shed (429/503)</span>
</div>
{rates_fig}
{lat_fig}
</section>

<section class="page">
{itl_fig}
<table class="num">
<thead><tr><th>offered</th><th>arrived/s</th><th>goodput/s</th><th>shed/s</th><th>shed %</th>
<th>TTFT p50</th><th>TTFT p95</th><th>ITL p50</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</section>

<section>
<h2>4 &nbsp; Two findings</h2>

<div class="finding">
<h3>The load harness mis-measures under overload</h3>
<p>Two independent faults in <code>loadtest/harness.py</code>, both of which deflate reported rates:</p>
<p><b>1. <code>_open_loop</code> under-offers.</b> It chains relative sleeps &mdash;
<code>create_task(...)</code> then <code>await asyncio.sleep(expovariate(rps))</code> &mdash; so each
iteration inherits the scheduling overshoot of every previous one. Once the event loop is busy that
compounds: a 32&nbsp;req/s target delivered about 12. Arrival times need to be absolute, so a late
wake-up is caught up on rather than carried forward.</p>
<p><b>2. <code>run_phase</code>'s window includes the drain.</b> The clock starts before the arrivals
and stops after <code>gather()</code> has waited for every straggler, so a 16-second arrival window
can be divided by roughly 26 seconds of wall clock. Every derived rate &mdash;
<code>throughput_rps</code>, <code>goodput_rps</code>, <code>output_tps</code> &mdash; is deflated,
by about 60% at these levels.</p>
<p>This is the tool behind the README's headline numbers. The <i>relative</i> story is unaffected
(numerator and denominator move together), but the absolute rates are not trustworthy under
overload. Both are fixable without changing the harness interface;
<code>demo/measure_inprocess.py</code> shows the corrected shape of each.</p>
</div>

<div class="finding">
<h3>Queue depth is a latency knob, not a capacity knob</h3>
<p>Swept at fixed concurrency (8 slots), 32&nbsp;req/s offered:</p>
<table class="num" style="width:105mm">
<thead><tr><th>max_queue_size</th><th>goodput</th><th>shed</th><th>TTFT p95</th></tr></thead>
<tbody>
<tr><td>8</td><td>4.3 req/s</td><td>82%</td><td>2.0&nbsp;s</td></tr>
<tr><td>16</td><td>4.3 req/s</td><td>79%</td><td>3.9&nbsp;s</td></tr>
<tr><td>64 &mdash; current&nbsp;default</td><td>4.3 req/s</td><td>64%</td><td>15.0&nbsp;s</td></tr>
</tbody>
</table>
<p style="margin-top:3mm">Goodput is identical across all three. A deeper queue buys no throughput
whatsoever; it only converts a fast refusal into a long wait. At a ~4&nbsp;req/s drain rate the
shipped default of 64 is a ~15-second queue &mdash; which is also exactly
<code>queue_timeout_s</code>, so the queue can only ever be fully drained at the timeout boundary.
The demo runs at 16 and says so.</p>
</div>

<div class="ask">
<b>Two decisions.</b> Whether to fix the harness &mdash; contained, but it changes numbers the README
quotes. And whether 64 is the right shipped default for <code>max_queue_size</code>.
</div>
</section>

<footer>Figures produced by <code>demo/measure_inprocess.py</code>, driving the real
<code>AdmissionController</code> and <code>MockEngine</code> in-process. The HTTP layer is not
exercised in the sweep; beat 2 of the demo uses the real server, so the 429s on screen are genuine
wire responses.</footer>
"""

out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/brief.html")
out.write_text(HTML, encoding="utf-8")
print(f"wrote {out} ({len(HTML)} bytes)")
