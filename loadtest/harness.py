"""Concurrency-sweeping load generator for streaming LLM endpoints.

Measures what actually matters for interactive serving:

* **TTFT** -- time to the first streamed token (what a user perceives as "it started").
* **ITL** -- gap between consecutive tokens (how fast the text scrolls).
* **E2E** -- full request duration.
* **Goodput** -- successful requests per second, as distinct from raw arrival rate.
* **Shed rate** -- what fraction admission control rejected, and how fast it said no.

Both load models are supported:

* *closed loop* (``--concurrency N``): N virtual users, each issuing the next request
  as soon as the previous finishes. Latency and throughput are coupled -- this is the
  right model for benchmarking a fixed client pool.
* *open loop* (``--rps R``): Poisson arrivals independent of how the server is coping.
  This is the model that exposes queueing collapse and proves admission control works,
  because the load does not politely back off when the server slows down.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

_WORDS = (
    "summarise the following incident report and list the mitigations that were "
    "applied describe the trade offs between batching and latency for transformer "
    "inference and explain how paged attention changes the memory picture"
).split()


# --------------------------------------------------------------------------- config
@dataclass
class LoadConfig:
    base_url: str = "http://127.0.0.1:8000"
    route: str = "chat"                      # "chat" | "completions"
    model: Optional[str] = None
    stream: bool = True
    concurrency: int = 8
    rps: Optional[float] = None              # set -> open loop
    requests: Optional[int] = None
    duration_s: float = 20.0
    warmup_s: float = 2.0
    max_tokens: int = 64
    prompt_words: int = 60
    timeout_s: float = 120.0
    api_key: Optional[str] = None
    seed: int = 7

    @property
    def path(self) -> str:
        return "/v1/chat/completions" if self.route == "chat" else "/v1/completions"


# -------------------------------------------------------------------------- results
@dataclass
class RequestOutcome:
    ok: bool
    status: int
    e2e_s: float
    ttft_s: Optional[float] = None
    tokens: int = 0
    itls: List[float] = field(default_factory=list)
    error: Optional[str] = None
    rejected: bool = False
    reject_reason: Optional[str] = None


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 100])."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * (q / 100.0)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


@dataclass
class PhaseReport:
    label: str
    concurrency: int
    rps_target: Optional[float]
    wall_s: float
    issued: int
    succeeded: int
    rejected: int
    failed: int
    throughput_rps: float
    goodput_rps: float
    output_tokens: int
    output_tps: float
    ttft: Dict[str, float]
    itl: Dict[str, float]
    e2e: Dict[str, float]
    reject_reasons: Dict[str, int]
    status_codes: Dict[str, int]
    server_stats: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _summarise(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


# ------------------------------------------------------------------------- requests
def _prompt(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(max(1, words)))


def _payload(cfg: LoadConfig, prompt: str) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "max_tokens": cfg.max_tokens,
        "temperature": 0.7,
        "stream": cfg.stream,
    }
    if cfg.model:
        body["model"] = cfg.model
    if cfg.route == "chat":
        body["messages"] = [{"role": "user", "content": prompt}]
    else:
        body["prompt"] = prompt
    return body


def _delta_text(obj: Dict[str, Any], route: str) -> str:
    choices = obj.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    if route == "chat":
        return (choice.get("delta") or {}).get("content") or ""
    return choice.get("text") or ""


async def _one_request(
    client: httpx.AsyncClient, cfg: LoadConfig, prompt: str
) -> RequestOutcome:
    payload = _payload(cfg, prompt)
    started = time.perf_counter()
    ttft: Optional[float] = None
    last: Optional[float] = None
    itls: List[float] = []
    tokens = 0

    try:
        if not cfg.stream:
            resp = await client.post(cfg.path, json=payload)
            elapsed = time.perf_counter() - started
            if resp.status_code != 200:
                return _rejection(resp, elapsed)
            body = resp.json()
            usage = body.get("usage") or {}
            return RequestOutcome(
                ok=True,
                status=200,
                e2e_s=elapsed,
                ttft_s=elapsed,
                tokens=int(usage.get("completion_tokens") or 0),
            )

        async with client.stream("POST", cfg.path, json=payload) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return _rejection(resp, time.perf_counter() - started)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in obj:
                    return RequestOutcome(
                        ok=False,
                        status=200,
                        e2e_s=time.perf_counter() - started,
                        ttft_s=ttft,
                        tokens=tokens,
                        itls=itls,
                        error=str(obj["error"].get("code", "stream_error")),
                    )
                text = _delta_text(obj, cfg.route)
                if not text:
                    continue
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - started
                elif last is not None:
                    itls.append(now - last)
                last = now
                tokens += 1
        return RequestOutcome(
            ok=True,
            status=200,
            e2e_s=time.perf_counter() - started,
            ttft_s=ttft,
            tokens=tokens,
            itls=itls,
        )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        return RequestOutcome(
            ok=False,
            status=0,
            e2e_s=time.perf_counter() - started,
            error=type(exc).__name__,
        )


def _rejection(resp: httpx.Response, elapsed: float) -> RequestOutcome:
    reason = None
    try:
        reason = ((resp.json() or {}).get("error") or {}).get("code")
    except Exception:
        pass
    shed = resp.status_code in (429, 503)
    return RequestOutcome(
        ok=False,
        status=resp.status_code,
        e2e_s=elapsed,
        rejected=shed,
        reject_reason=reason or f"http_{resp.status_code}",
        error=None if shed else f"http_{resp.status_code}",
    )


# ---------------------------------------------------------------------- load models
async def _closed_loop(
    client: httpx.AsyncClient, cfg: LoadConfig, deadline: float, budget: Optional[int]
) -> List[RequestOutcome]:
    outcomes: List[RequestOutcome] = []
    remaining = budget
    lock = asyncio.Lock()

    async def worker(worker_id: int) -> None:
        nonlocal remaining
        rng = random.Random(cfg.seed + worker_id)
        while time.perf_counter() < deadline:
            async with lock:
                if remaining is not None:
                    if remaining <= 0:
                        return
                    remaining -= 1
            outcomes.append(await _one_request(client, cfg, _prompt(rng, cfg.prompt_words)))

    await asyncio.gather(*(worker(i) for i in range(cfg.concurrency)))
    return outcomes


async def _open_loop(
    client: httpx.AsyncClient, cfg: LoadConfig, deadline: float, budget: Optional[int]
) -> List[RequestOutcome]:
    """Poisson arrivals: the generator does not slow down when the server does."""
    outcomes: List[RequestOutcome] = []
    rng = random.Random(cfg.seed)
    tasks: List[asyncio.Task] = []
    issued = 0
    assert cfg.rps and cfg.rps > 0

    async def fire(prompt: str) -> None:
        outcomes.append(await _one_request(client, cfg, prompt))

    while time.perf_counter() < deadline and (budget is None or issued < budget):
        tasks.append(asyncio.create_task(fire(_prompt(rng, cfg.prompt_words))))
        issued += 1
        await asyncio.sleep(rng.expovariate(cfg.rps))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return outcomes


async def _fetch_stats(client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get("/stats")
        if resp.status_code == 200:
            return resp.json().get("admission")
    except httpx.HTTPError:
        return None
    return None


# ------------------------------------------------------------------------- run loop
async def run_phase(cfg: LoadConfig, label: Optional[str] = None) -> PhaseReport:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    limits = httpx.Limits(
        max_connections=max(cfg.concurrency * 2, 64),
        max_keepalive_connections=max(cfg.concurrency * 2, 64),
    )

    async with httpx.AsyncClient(
        base_url=cfg.base_url, headers=headers, timeout=cfg.timeout_s, limits=limits
    ) as client:
        if cfg.warmup_s > 0:
            warm = LoadConfig(**{**asdict(cfg), "duration_s": cfg.warmup_s, "warmup_s": 0.0})
            await _closed_loop(
                client, warm, time.perf_counter() + cfg.warmup_s, budget=None
            )

        started = time.perf_counter()
        deadline = started + cfg.duration_s
        if cfg.rps:
            outcomes = await _open_loop(client, cfg, deadline, cfg.requests)
        else:
            outcomes = await _closed_loop(client, cfg, deadline, cfg.requests)
        wall = time.perf_counter() - started
        server_stats = await _fetch_stats(client)

    return _build_report(cfg, label or _default_label(cfg), outcomes, wall, server_stats)


def _default_label(cfg: LoadConfig) -> str:
    return f"rps={cfg.rps:g}" if cfg.rps else f"c={cfg.concurrency}"


def _build_report(
    cfg: LoadConfig,
    label: str,
    outcomes: List[RequestOutcome],
    wall: float,
    server_stats: Optional[Dict[str, Any]],
) -> PhaseReport:
    ok = [o for o in outcomes if o.ok]
    rejected = [o for o in outcomes if o.rejected]
    failed = [o for o in outcomes if not o.ok and not o.rejected]

    ttfts = [o.ttft_s for o in ok if o.ttft_s is not None]
    e2es = [o.e2e_s for o in ok]
    itls = [gap for o in ok for gap in o.itls]
    tokens = sum(o.tokens for o in ok)

    reasons: Dict[str, int] = {}
    for o in rejected:
        reasons[o.reject_reason or "unknown"] = reasons.get(o.reject_reason or "unknown", 0) + 1
    codes: Dict[str, int] = {}
    for o in outcomes:
        codes[str(o.status)] = codes.get(str(o.status), 0) + 1

    return PhaseReport(
        label=label,
        concurrency=cfg.concurrency,
        rps_target=cfg.rps,
        wall_s=wall,
        issued=len(outcomes),
        succeeded=len(ok),
        rejected=len(rejected),
        failed=len(failed),
        throughput_rps=len(outcomes) / wall if wall else 0.0,
        goodput_rps=len(ok) / wall if wall else 0.0,
        output_tokens=tokens,
        output_tps=tokens / wall if wall else 0.0,
        ttft=_summarise(ttfts),
        itl=_summarise(itls),
        e2e=_summarise(e2es),
        reject_reasons=reasons,
        status_codes=codes,
        server_stats=server_stats,
    )


async def run_sweep(cfg: LoadConfig, levels: Sequence[float], open_loop: bool) -> List[PhaseReport]:
    reports: List[PhaseReport] = []
    for level in levels:
        phase = LoadConfig(**asdict(cfg))
        if open_loop:
            phase.rps = float(level)
        else:
            phase.concurrency = int(level)
            phase.rps = None
        reports.append(await run_phase(phase))
        await asyncio.sleep(0.5)  # let the server settle between phases
    return reports


# -------------------------------------------------------------------------- report
def _fmt_ms(summary: Dict[str, float], key: str) -> str:
    value = summary.get(key)
    if value is None or summary.get("n", 0) == 0:
        return "-"
    return f"{value * 1000:.0f}"


def format_table(reports: Sequence[PhaseReport]) -> str:
    header = (
        f"{'level':>10} {'sent':>6} {'ok':>6} {'shed':>6} {'err':>5} "
        f"{'gput/s':>8} {'tok/s':>8} {'ttft p50':>9} {'ttft p95':>9} "
        f"{'itl p50':>8} {'e2e p95':>9}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        lines.append(
            f"{r.label:>10} {r.issued:>6} {r.succeeded:>6} {r.rejected:>6} {r.failed:>5} "
            f"{r.goodput_rps:>8.1f} {r.output_tps:>8.0f} "
            f"{_fmt_ms(r.ttft, 'p50'):>9} {_fmt_ms(r.ttft, 'p95'):>9} "
            f"{_fmt_ms(r.itl, 'p50'):>8} {_fmt_ms(r.e2e, 'p95'):>9}"
        )
    lines.append("")
    lines.append("latencies in ms; gput = successful requests/s; shed = 429/503 from admission control")
    return "\n".join(lines)
