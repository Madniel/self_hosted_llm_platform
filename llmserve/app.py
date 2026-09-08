"""Application factory: lifespan, middleware, error mapping, routes."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import metrics
from .admission import AdmissionController
from .config import Settings, get_settings
from .engine import build_engine
from .engine.base import InferenceEngine
from .errors import InvalidRequestError, ServiceError
from .logging_setup import configure_logging, new_request_id, request_id_var
from .routes import inference_router, ops_router
from .service import AppContext


logger = logging.getLogger(__name__)


def _error_response(exc: ServiceError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(max(1, round(exc.retry_after))))
    rid = request_id_var.get()
    if rid:
        headers["X-Request-Id"] = rid
    return JSONResponse(
        status_code=exc.http_status, content=exc.to_payload(), headers=headers
    )


def create_app(
    settings: Settings | None = None,
    *,
    engine: InferenceEngine | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the ASGI app.

    ``engine`` can be injected by tests to swap in a fake without touching config.
    """
    settings = settings or get_settings()
    if configure_logs:
        configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Own the engine and the admission controller for the process lifetime.

        Shutdown order matters and is the reason this is explicit: admission closes
        *first* so ``/readyz`` starts failing and the load balancer drains us, then
        in-flight generations are given ``drain_timeout_s`` to finish streaming, and
        only then is the engine torn down. Tearing the engine down first would kill
        responses that were already mid-flight to a client.
        """
        inference_engine = engine or build_engine(settings)

        def on_change(in_flight: int, queued: int) -> None:
            metrics.in_flight.set(in_flight)
            metrics.queue_depth.set(queued)

        admission = AdmissionController(
            max_concurrent=settings.max_concurrent_requests,
            max_queue_size=settings.max_queue_size,
            queue_timeout_s=settings.queue_timeout_s,
            on_change=on_change,
        )
        metrics.capacity.set(settings.max_concurrent_requests)
        app.state.ctx = AppContext(
            settings=settings, engine=inference_engine, admission=admission
        )

        logger.info("starting engine", extra={"config": settings.describe()})
        started = time.perf_counter()
        await inference_engine.startup()
        metrics.engine_up.set(1)
        logger.info(
            "engine ready",
            extra={
                "model": inference_engine.model_id,
                "backend": settings.backend,
                "load_s": round(time.perf_counter() - started, 3),
                "max_concurrency": settings.max_concurrent_requests,
                "max_queue_size": settings.max_queue_size,
            },
        )
        try:
            yield
        finally:
            # Stop admitting first so /readyz flips and the LB drains us, then wait
            # for in-flight generations before tearing the engine down.
            drained = await admission.close(settings.drain_timeout_s)
            if not drained:
                logger.warning(
                    "drain timed out", extra={"in_flight": admission.in_flight}
                )
            metrics.engine_up.set(0)
            await inference_engine.shutdown()
            logger.info("shutdown complete", extra={"admission": admission.snapshot()})

    app = FastAPI(
        title="Self-Hosted LLM Serving Platform",
        version="0.1.0",
        summary="FastAPI + vLLM inference with token streaming and admission control",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Bind a request id to the context for the duration of the request.

        An inbound ``X-Request-Id`` is honoured so a correlation id assigned upstream
        survives into this service's logs; otherwise one is minted here. The id is
        echoed back on the response alongside the server-side processing time.
        """
        rid = request.headers.get("x-request-id") or new_request_id()
        token = request_id_var.set(rid)
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ())[1:]) or 'body'}: {err.get('msg')}"
            for err in exc.errors()
        )
        return _error_response(InvalidRequestError(detail or "invalid request body"))

    app.include_router(ops_router)
    app.include_router(inference_router)
    return app


app_factory = create_app
