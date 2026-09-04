# Self-Hosted LLM Serving Platform

A FastAPI inference server for vLLM with async token streaming, bounded concurrency and
explicit admission control — plus a load-test harness that measures time-to-first-token,
inter-token latency, goodput and shed rate.

The point of the project is what happens *past* saturation. An LLM engine has a fixed
KV-cache and compute budget; hand it every arriving request and you get the classic
unbounded-queue collapse, where everyone's latency degrades together and clients time out
on work the server is still paying for. So requests are admitted through a bounded FIFO
queue, overload is refused in microseconds with `429 + Retry-After` *before* the response
starts, and anything that waits too long is shed rather than served late.

Open-loop load test — Poisson arrivals against 8 execution slots, mock backend:

```
     level   sent     ok   shed   err   gput/s   ttft p50  ttft p95  itl p50
---------------------------------------------------------------------------
     rps=2     13     13      0     0      1.9        159       197       21
     rps=5     39     39      0     0      4.6        636      1727       26
    rps=10     65     65      0     0      5.2       3297      5220       26
    rps=20    126     68     58     0      5.2       4735      5975       26
    rps=40    236     69    167     0      5.3       5281      6009       26
```

At 4× and 8× the sustainable rate the server sheds 46% and 71% of arrivals — and `err`
stays 0. Goodput holds flat instead of collapsing, and every refused client got a fast
429 rather than a hung socket.

## Quickstart

No GPU required — the `mock` backend models prefill cost, jittered inter-token latency
and decode slowdown under batch contention, so the whole serving path is exercisable on a
laptop and in CI.

```bash
make install
make test          # 54 tests
make run           # OpenAI-compatible server on :8000
```

```bash
make smoke         # one streamed completion
make load-open     # open-loop sweep — reproduces the table above
```

On Windows use `.\scripts\tasks.ps1` instead of `make`; see
[docs/deployment.md](docs/deployment.md).

## API

OpenAI-compatible, so `openai-python`, LangChain and existing clients work unchanged.

| Method | Path | |
|---|---|---|
| `POST` | `/v1/completions`, `/v1/chat/completions` | `stream: true` for SSE |
| `GET` | `/healthz`, `/readyz` | liveness / readiness |
| `GET` | `/metrics`, `/stats` | Prometheus, admission counters |
| `POST` | `/admin/drain` | pre-stop hook: stop accepting, finish in-flight work |

Overload returns `429 queue_full`; a slot that never frees returns `503 queue_timeout`;
both carry `Retry-After`.

## Running a real model

```bash
pip install -r requirements-gpu.txt          # CUDA host
LLMSERVE_BACKEND=vllm LLMSERVE_MODEL=meta-llama/Llama-3.1-8B-Instruct make run
```

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | admission control design, API reference, metrics, config, tests |
| [deployment.md](docs/deployment.md) | Ubuntu + systemd, Docker, WSL, Windows, nginx, graceful shutdown |
| [load-testing.md](docs/load-testing.md) | the harness, both load models, full measured results |
| [tuning.md](docs/tuning.md) | sizing the concurrency and queue bounds, alerts, runbook |

## Layout

```
llmserve/
  admission.py     FairSemaphore + AdmissionController   ← the load-bearing piece
  service.py       admission → engine → metered stream
  streaming.py     SSE framing, keep-alive, latency recorder
  routes/          OpenAI-compatible endpoints + ops endpoints
  engine/          base contract, vLLM adapter, mock backend
loadtest/          closed-loop and open-loop generator, percentile reporting
tests/             54 tests, concentrated on the concurrency and overload paths
deploy/            systemd unit + EnvironmentFile example
scripts/           demo, PowerShell task runner, minimal SSE client
```
