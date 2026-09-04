"""Entry point: ``python -m llmserve``."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,      # logging_setup owns the handlers
        access_log=False,     # the request-context middleware logs instead
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
