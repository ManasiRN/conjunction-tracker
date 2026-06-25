"""Service entrypoint.

Configures logging, builds the FastAPI app (which on startup runs the optional
initial fetch and starts the background scheduler), and serves it with uvicorn.

Run locally:   python -m app.main
In Docker:     CMD ["python", "-m", "app.main"]
"""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import get_settings


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


settings = get_settings()
_configure_logging(settings.log_level)

# Module-level app so `uvicorn app.main:app` also works.
app = create_app(settings)


def main() -> None:
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
