from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from anki.cards import Card
from anki.consts import (
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
)
from anki.scheduler.v3 import Scheduler as V3Scheduler
from anki.scheduler_pb2 import CardAnswer
from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer


# ============================================================================
# Learn & Review
# ============================================================================

ADDON_DIR = Path(__file__).resolve().parent

LOG_FILE = ADDON_DIR / "learn-review.log"

logger = logging.getLogger("LearnReview")
logger.setLevel(logging.INFO)
logger.handlers.clear()

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=1_000_000,
    backupCount=3,
    encoding="utf-8",
)

file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
)

logger.addHandler(file_handler)
logger.propagate = False


def log(message: str) -> None:
    logger.info(message)


def log_exception(message: str) -> None:
    logger.error(message)
    logger.error(traceback.format_exc())


log("============================================================")
log("Learn & Review - START")
log("============================================================")
log(f"Add-on directory: {ADDON_DIR}")
log(f"Log file: {LOG_FILE}")
log("Log rotation: max_bytes=1000000 backup_count=3")


# ============================================================================
# Session state
# ============================================================================

from . import session
from .session import ReviewSession


# ============================================================================
# Scheduler helpers
# ============================================================================

from . import scheduler

scheduler.init_logger(log=log, log_exception=log_exception)


# ============================================================================
# Native Reviewer integration
# ============================================================================

from . import reviewer

reviewer.init_logger(log=log, log_exception=log_exception)
reviewer.apply_patches()


# ============================================================================
# Session Manager & Deck Browser integration
# ============================================================================

from . import deck_browser, session_manager

session_manager.init_logger(log=log, log_exception=log_exception)
session_manager.register_hooks()

deck_browser.initialize(
    get_today_counts=scheduler.get_today_counts,
    start_session=session_manager.start_session,
    log=log,
    log_exception=log_exception,
)


# ============================================================================
# Diagnostics
# ============================================================================

def on_main_window_init() -> None:

    log(
        "MAIN_WINDOW_INIT"
    )

    try:
        if mw.col is None:
            log(
                "COLLECTION_READY=False"
            )
            return

        log(
            "COLLECTION_READY=True"
        )

        try:
            decks = (
                mw.col.decks.all_names_and_ids()
            )

            log(
                f"DECK_COUNT={len(decks)}"
            )

        except Exception:
            log_exception(
                "DECK_COUNT_ERROR"
            )

        try:
            log(
                f"V3_SCHEDULER="
                f"{mw.col.v3_scheduler()}"
            )

            log(
                f"SCHEDULER_VERSION="
                f"{mw.col.sched_ver()}"
            )

        except Exception:
            log_exception(
                "SCHEDULER_INFO_ERROR"
            )

    except Exception:
        log_exception(
            "MAIN_WINDOW_INIT_ERROR"
        )


gui_hooks.main_window_did_init.append(
    on_main_window_init
)


def on_state_did_change(
    new_state: str,
    old_state: str,
) -> None:

    log(
        f"STATE_DID_CHANGE "
        f"old={old_state!r} "
        f"new={new_state!r}"
    )


gui_hooks.state_did_change.append(
    on_state_did_change
)


# ============================================================================
# Ready
# ============================================================================

log(
    "Reviewer._get_next_v3_card patched"
)

log(
    "Reviewer._answerCard patched with GradeNow"
)

log(
    "Session semantics: initial scope frozen"
)

log(
    "Learn semantics: New cards only, one pass per card"
)

log(
    "Review semantics: Learning + Review + Relearning"
)

log(
    "Review semantics: cards can be revisited when native scheduler "
    "makes them immediately available"
)

log(
    "Review reinsertion restricted to initial session scope"
)

log(
    "Native scheduler decides Review card re-availability"
)

log(
    "Deck limits and scheduler configuration are never modified"
)

log(
    "No filtered decks are created"
)

log(
    "Deck Browser integration initialized"
)

log(
    "Dedicated Learn & Review column installed"
)

log(
    "Native Options column preserved separately"
)

log(
    "JS command hook installed"
)

log(
    "Reviewer answer hook installed"
)

log(
    "State hooks installed"
)

log(
    "Learn & Review - READY"
)