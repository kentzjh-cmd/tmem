"""tmem: Invisible Agent Execution Trace Compiler & Repository-Scoped Memory Engine."""

from __future__ import annotations

import logging
import os

__version__ = "0.2.0"

logger = logging.getLogger("tmem")

# Enable debug logging if TMEM_DEBUG is set
if os.environ.get("TMEM_DEBUG") == "1":
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] [tmem] %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
else:
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
