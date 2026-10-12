"""`frootio` console script: run the app with uvicorn on FROOTIO_HOST:FROOTIO_PORT."""

from __future__ import annotations

import logging

import uvicorn

from . import config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.settings()
    # SSE streams never finish on their own; don't let them hold up shutdown.
    uvicorn.run("frootio.app:create_app", factory=True, host=s.host, port=s.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
