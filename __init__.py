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
# Start a session
# ============================================================================

def start_session(
    mode: str,
    deck_id: int,
) -> None:

    if mw.col is None:
        raise RuntimeError(
            "No Anki collection is open."
        )

    if mode not in (
        "learn",
        "review",
    ):
        raise ValueError(
            f"Invalid session mode: {mode!r}"
        )

    deck = mw.col.decks.get(
        deck_id
    )

    if not deck:
        raise RuntimeError(
            f"Deck not found: {deck_id}"
        )

    deck_name = str(
        deck["name"]
    )

    log(
        f"START_SESSION "
        f"mode={mode} "
        f"deck_id={deck_id} "
        f"deck_name={deck_name!r}"
    )

    mw.col.decks.select(
        deck_id
    )

    card_ids = scheduler.get_today_card_ids(
        mode,
        deck_id,
    )

    log(
        f"START_SESSION_SNAPSHOT "
        f"mode={mode} "
        f"deck_id={deck_id} "
        f"cards={len(card_ids)}"
    )

    if not card_ids:
        log(
            f"START_SESSION_ABORTED "
            f"mode={mode} "
            f"deck_id={deck_id} "
            f"reason=no_available_cards"
        )

        return

    new_session = ReviewSession(
        mode=mode,
        deck_id=deck_id,
        deck_name=deck_name,
        card_ids=list(card_ids),
        scope_card_ids=set(card_ids),
        position=0,
    )
    session.set_session(new_session)

    log(
        f"SESSION_CREATED "
        f"mode={mode} "
        f"deck_id={deck_id} "
        f"cards={len(card_ids)} "
        f"scope={len(card_ids)} "
        f"snapshot_frozen=yes "
        f"dynamic_review_reinsert=yes"
    )

    try:
        mw.moveToState(
            "review"
        )

        log(
            "MOVE_TO_REVIEW complete"
        )

    except Exception:
        session.clear_session()

        log_exception(
            "MOVE_TO_REVIEW_ERROR"
        )

        raise


# ============================================================================
# Deck Browser integration
# ============================================================================

from . import deck_browser

deck_browser.initialize(
    get_today_counts=scheduler.get_today_counts,
    start_session=start_session,
    log=log,
    log_exception=log_exception,
)


# ============================================================================
# Synchronize session after answering
# ============================================================================

def on_reviewer_did_answer_card(
    reviewer: Reviewer,
    card: Card,
    ease: int,
) -> None:

    current_session = session.get_session()

    if current_session is None:
        return

    try:
        active_session = current_session

        log(
            f"ANSWERED "
            f"card_id={card.id} "
            f"ease={ease} "
            f"mode={active_session.mode} "
            f"session_position={active_session.position} "
            f"session_remaining_before={active_session.remaining()}"
        )

        current_id = (
            active_session.current_card_id()
        )

        if current_id != card.id:
            log(
                f"SESSION_POSITION_MISMATCH "
                f"expected={current_id} "
                f"answered={card.id}"
            )

            return

        active_session.advance()

        log(
            f"SESSION_ADVANCE "
            f"answered_card={card.id} "
            f"new_position={active_session.position} "
            f"remaining={active_session.remaining()}"
        )

        # ----------------------------------------------------------------
        # Learn
        # ----------------------------------------------------------------

        if active_session.mode == "learn":
            log(
                f"SESSION_REINSERT "
                f"mode=learn "
                f"card_id={card.id} "
                f"reinserted=no "
                f"reason=learn_one_pass"
            )

            return

        # ----------------------------------------------------------------
        # Review
        # ----------------------------------------------------------------

        if active_session.mode == "review":

            if not active_session.is_in_scope(
                card.id
            ):
                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=no "
                    f"reason=outside_initial_scope"
                )

                return

            try:
                card.load()

            except Exception:
                log_exception(
                    f"SESSION_REINSERT_CARD_RELOAD_ERROR "
                    f"card_id={card.id}"
                )

            if card.queue < 0:
                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=no "
                    f"reason=suspended "
                    f"queue={card.queue}"
                )

                return

            currently_available = (
                scheduler.is_card_currently_queued(
                    active_session.deck_id,
                    int(card.id),
                )
            )

            if currently_available:
                active_session.append_revisit(
                    int(card.id)
                )

                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=yes "
                    f"reason=native_scheduler_available "
                    f"new_total={len(active_session.card_ids)} "
                    f"new_remaining={active_session.remaining()}"
                )

            else:
                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=no "
                    f"reason=native_scheduler_not_available "
                    f"new_remaining={active_session.remaining()}"
                )

    except Exception:
        log_exception(
            f"SESSION_ADVANCE_ERROR "
            f"card_id={card.id}"
        )


gui_hooks.reviewer_did_answer_card.append(
    on_reviewer_did_answer_card
)


# ============================================================================
# Session cleanup
# ============================================================================

def on_state_will_change(
    new_state: str,
    old_state: str,
) -> None:

    if (
        old_state == "review"
        and new_state != "review"
    ):
        current_session = session.get_session()
        if current_session is not None:
            log(
                f"SESSION_ABORTED "
                f"state_change "
                f"old={old_state!r} "
                f"new={new_state!r} "
                f"position={current_session.position} "
                f"total={len(current_session.card_ids)} "
                f"scope={len(current_session.scope_card_ids)}"
            )

            session.clear_session()


gui_hooks.state_will_change.append(
    on_state_will_change
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