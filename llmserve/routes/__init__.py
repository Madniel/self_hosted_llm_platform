from .completions import router as inference_router
from .health import router as ops_router

__all__ = ["inference_router", "ops_router"]
