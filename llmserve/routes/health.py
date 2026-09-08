"""Liveness, readiness, model listing, metrics and an admission-control snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import metrics as m
from ..schemas import HealthResponse, ModelCard, ModelList, StatsResponse
from ..service import AppContext
from .deps import get_ctx, require_api_key


router = APIRouter(tags=["ops"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz(ctx: AppContext = Depends(get_ctx)) -> HealthResponse:
    """Liveness: the process is up and serving HTTP."""
    return HealthResponse(
        status="ok",
        model=ctx.model_id,
        backend=ctx.settings.backend,
        engine_ready=ctx.engine.ready,
    )


@router.get("/readyz")
async def readyz(response: Response, ctx: AppContext = Depends(get_ctx)) -> dict:
    """Readiness: weights loaded and admission control still accepting.

    Returns 503 while the model is loading or during graceful shutdown, so a load
    balancer stops sending traffic before the process actually goes away.
    """
    ready = ctx.engine.ready and not ctx.admission.closed
    if not ready:
        response.status_code = 503
    return {
        "ready": ready,
        "engine_ready": ctx.engine.ready,
        "accepting": not ctx.admission.closed,
        "in_flight": ctx.admission.in_flight,
        "queue_depth": ctx.admission.queue_depth,
    }


@router.get("/v1/models", response_model=ModelList)
async def list_models(ctx: AppContext = Depends(get_ctx)) -> ModelList:
    return ModelList(data=[ModelCard(id=ctx.model_id)])


@router.get("/stats", response_model=StatsResponse)
async def stats(ctx: AppContext = Depends(get_ctx)) -> StatsResponse:
    """Human-readable counterpart to /metrics: what admission control has been doing."""
    return StatsResponse(
        model=ctx.model_id,
        backend=ctx.settings.backend,
        admission=ctx.admission.snapshot(),
    )


@router.get("/metrics")
async def prometheus_metrics(ctx: AppContext = Depends(get_ctx)) -> Response:
    if not ctx.settings.enable_metrics:
        return Response(status_code=404)
    body, content_type = m.render()
    return Response(content=body, media_type=content_type)


@router.post("/admin/drain", dependencies=[Depends(require_api_key)])
async def drain(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Pre-stop hook: stop accepting, keep serving what is already running.

    Kubernetes sends SIGTERM and removes the pod from the Service at roughly the same
    time, which races: requests can still arrive after the process starts shutting
    down. Calling this from a ``preStop`` hook flips /readyz to 503 first, so the load
    balancer drains the pod while in-flight generations finish streaming.
    """
    rejected = ctx.admission.stop_accepting()
    return {
        "accepting": False,
        "queued_rejected": rejected,
        "in_flight": ctx.admission.in_flight,
    }
