"""`python -m backend` - startet uvicorn mit Host/Port aus der Konfiguration."""
from __future__ import annotations

import logging
import sys

import uvicorn

from .config import ConfigError, load_config
from .main import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"\nFEHLER: {exc}\n", file=sys.stderr)
        return 1
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
