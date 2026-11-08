from __future__ import annotations

import logging
import time

import uvicorn
from dotenv import load_dotenv

from modules.multimodal.app import create_app
from modules.multimodal.config import Settings


def main() -> None:
    load_dotenv()
    settings = Settings.from_env()
    # Uvicorn configures its own loggers, not the bridge's application logger.
    logger = logging.getLogger("mm_bridge")
    log_level = settings.log_level.upper()
    logger.setLevel(5 if log_level == "TRACE" else log_level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.bridge_host,
        port=settings.bridge_port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
