from __future__ import annotations

import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent
LOG_FILE = ADDON_DIR / "learn-review.log"

DEFAULT_LOG_LEVEL = logging.INFO

logger = logging.getLogger("LearnReview")
logger.setLevel(DEFAULT_LOG_LEVEL)
logger.handlers.clear()

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)

file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
)

logger.addHandler(file_handler)
logger.propagate = False


def log(message: str) -> None:
    logger.info(message)


def log_debug(message: str) -> None:
    logger.debug(message)


def log_exception(message: str) -> None:
    logger.error(message)
    logger.error(traceback.format_exc())
