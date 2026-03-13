"""
Structured logging with file rotation and console output.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5

_configured = False


def _configure_root(level: str = "INFO", log_file: str = "perp_arb_bot.log"):
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger("perp_arb")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(ch)

    # File handler with rotation
    log_path = Path(__file__).parent.parent / log_file
    fh = RotatingFileHandler(log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the perp_arb namespace."""
    _configure_root()
    return logging.getLogger(f"perp_arb.{name}")
