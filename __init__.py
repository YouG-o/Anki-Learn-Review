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


@dataclass
class SessionContext:
    deck_name: str


@dataclass
class SessionV3Info:
    """
    Compatibility object implementing the subset of Reviewer.V3CardInfo
    used by the native Reviewer.

    The actual card scheduling remains entirely native to Anki.
    """

    states: Any
    context: SessionContext
    remaining_new: int
    remaining_learning: int
    remaining_review: int
    mode: str

    def counts(self) -> tuple[int, list[int]]:
        if self.mode == "learn":
            return (
                0,
                [
                    self.remaining_new,
                    self.remaining_learning,
                    self.remaining_review,
                ],
            )

        return (
            1,
            [
                self.remaining_new,
                self.remaining_learning,
                self.remaining_review,
            ],
        )

    @staticmethod
    def rating_from_ease(ease: int) -> Any:
        if ease == 1:
            return CardAnswer.AGAIN

        if ease == 2:
            return CardAnswer.HARD

        if ease == 3:
            return CardAnswer.GOOD

        if ease == 4:
            return CardAnswer.EASY

        raise ValueError(
            f"Invalid ease: {ease}"
        )


# ============================================================================
# Scheduler helpers
# ============================================================================

from . import scheduler

scheduler.init_logger(log=log, log_exception=log_exception)


# ============================================================================
# Native Reviewer integration
# ============================================================================

ORIGINAL_GET_NEXT_V3_CARD = (
    Reviewer._get_next_v3_card
)

ORIGINAL_ANSWER_CARD = (
    Reviewer._answerCard
)


def get_custom_scheduling_info(
    reviewer: Reviewer,
    card: Card,
    session: ReviewSession,
) -> SessionV3Info:
    """
    Obtain native scheduling states for the displayed card.

    The native states determine the actual Again / Hard / Good / Easy
    intervals shown by the Reviewer.

    Actual grading is performed through GradeNow.
    """

    states = scheduler.get_backend_scheduling_states(card.id)

    try:
        states.current.custom_data = (
            card.custom_data
        )

    except Exception:
        log(
            "STATE_CUSTOM_DATA_ASSIGN_FAILED "
            f"card_id={card.id}"
        )

    remaining = session.remaining()

    if session.mode == "learn":
        return SessionV3Info(
            states=states,
            context=SessionContext(
                deck_name=session.deck_name
            ),
            remaining_new=remaining,
            remaining_learning=0,
            remaining_review=0,
            mode="learn",
        )

    return SessionV3Info(
        states=states,
        context=SessionContext(
            deck_name=session.deck_name
        ),
        remaining_new=0,
        remaining_learning=remaining,
        remaining_review=0,
        mode="review",
    )


def card_matches_session(
    card: Card,
    mode: str,
) -> bool:
    """
    Safety check applied when taking a card from the session worklist.
    """

    if card.queue < 0:
        return False

    if mode == "learn":
        return (
            card.type == 0
            and card.queue
            == int(QUEUE_TYPE_NEW)
        )

    if mode == "review":
        return (
            card.queue
            in {
                int(QUEUE_TYPE_LRN),
                int(QUEUE_TYPE_REV),
                int(QUEUE_TYPE_DAY_LEARN_RELEARN),
            }
        )

    return False


def grade_card_now(
    reviewer: Reviewer,
    card: Card,
    ease: int,
) -> Any:
    """
    Grade the current card by ID through Anki's native GradeNow API.

    This is intentionally used instead of answer_card() because the cards
    selected by Learn & Review are a session worklist and therefore are
    not necessarily the scheduler's current top card.
    """

    rating_map = {
        1: CardAnswer.AGAIN,
        2: CardAnswer.HARD,
        3: CardAnswer.GOOD,
        4: CardAnswer.EASY,
    }

    try:
        rating = rating_map[
            ease
        ]

    except KeyError:
        raise ValueError(
            f"Invalid ease: {ease}"
        )

    log(
        f"GRADE_NOW "
        f"card_id={card.id} "
        f"ease={ease} "
        f"rating={int(rating)}"
    )

    changes = mw.col._backend.grade_now(
        card_ids=[
            int(card.id)
        ],
        rating=int(rating),
    )

    log(
        f"GRADE_NOW_COMPLETE "
        f"card_id={card.id}"
    )

    return changes


def custom_answer_card(
    self: Reviewer,
    ease: int,
) -> None:
    """
    Replace Reviewer._answerCard() only while a Learn & Review session
    is active.

    Outside Learn & Review sessions, native Anki behavior is untouched.
    """

    current_session = session.get_session()

    if current_session is None:
        return ORIGINAL_ANSWER_CARD(
            self,
            ease,
        )

    if self.mw.state != "review":
        return

    if self.state != "answer":
        return

    if self.card is None:
        log(
            "ANSWER_ABORTED no current card"
        )
        return

    card = self.card

    log(
        f"SESSION_ANSWER_REQUEST "
        f"mode={current_session.mode} "
        f"card_id={card.id} "
        f"ease={ease} "
        f"position={current_session.position} "
        f"remaining_before={current_session.remaining()}"
    )

    proceed, ease = (
        gui_hooks.reviewer_will_answer_card(
            (True, ease),
            self,
            card,
        )
    )

    if not proceed:
        log(
            f"SESSION_ANSWER_CANCELLED "
            f"card_id={card.id}"
        )

        return

    try:
        sched = scheduler.ensure_v3_scheduler()

        rating = (
            SessionV3Info.rating_from_ease(
                ease
            )
        )

        states = self._v3.states

        if rating == CardAnswer.AGAIN:
            new_state = states.again

        elif rating == CardAnswer.HARD:
            new_state = states.hard

        elif rating == CardAnswer.GOOD:
            new_state = states.good

        elif rating == CardAnswer.EASY:
            new_state = states.easy

        else:
            raise ValueError(
                f"Unsupported rating: {rating}"
            )

        is_leech = sched.state_is_leech(
            new_state
        )

        self.state = "transition"

        grade_card_now(
            self,
            card,
            ease,
        )

        card.load()

        suspended = (
            self.card is not None
            and self.card.queue < 0
        )

        log(
            f"SESSION_ANSWERED "
            f"mode={current_session.mode} "
            f"card_id={card.id} "
            f"ease={ease} "
            f"type={card.type} "
            f"queue={card.queue} "
            f"due={card.due}"
        )

        self._after_answering(
            ease
        )

        if is_leech:
            self.onLeech(
                suspended
            )

    except Exception:
        log_exception(
            f"SESSION_GRADE_NOW_ERROR "
            f"card_id={card.id} "
            f"ease={ease}"
        )

        self.state = "answer"


Reviewer._get_next_v3_card = (
    lambda self: custom_get_next_v3_card(self)
)

Reviewer._answerCard = (
    custom_answer_card
)


def custom_get_next_v3_card(
    self: Reviewer,
) -> None:
    """
    Replacement for Reviewer._get_next_v3_card() while a Learn & Review
    session is active.

    Learn:
        Strict one-pass behavior.

    Review:
        Initial scope is frozen, but cards from that scope may be appended
        again when the native scheduler says they are immediately available.
    """

    current_session = session.get_session()

    if current_session is None:
        return ORIGINAL_GET_NEXT_V3_CARD(
            self
        )

    log(
        f"REVIEWER_NEXT "
        f"mode={current_session.mode} "
        f"deck_id={current_session.deck_id} "
        f"position={current_session.position} "
        f"total={len(current_session.card_ids)} "
        f"remaining={current_session.remaining()} "
        f"scope={len(current_session.scope_card_ids)}"
    )

    while not current_session.finished():

        card_id = (
            current_session.current_card_id()
        )

        if card_id is None:
            break

        try:
            card = mw.col.get_card(
                card_id
            )

            if card is None:
                log(
                    f"SESSION_CARD_MISSING "
                    f"card_id={card_id}"
                )

                current_session.advance()
                continue

            if not card_matches_session(
                card,
                current_session.mode,
            ):
                log(
                    f"SESSION_CARD_SKIPPED "
                    f"card_id={card_id} "
                    f"mode={current_session.mode} "
                    f"type={card.type} "
                    f"queue={card.queue}"
                )

                current_session.advance()
                continue

            states_info = (
                get_custom_scheduling_info(
                    self,
                    card,
                    current_session,
                )
            )

            self._v3 = states_info
            self.card = card

            self.card.start_timer()

            log(
                f"SESSION_CARD_SELECTED "
                f"card_id={card.id} "
                f"deck_id={card.did} "
                f"type={card.type} "
                f"queue={card.queue} "
                f"due={card.due} "
                f"position={current_session.position} "
                f"remaining={current_session.remaining()}"
            )

            return

        except Exception:
            log_exception(
                f"SESSION_CARD_LOAD_ERROR "
                f"card_id={card_id}"
            )

            current_session.advance()

    log(
        f"SESSION_FINISHED "
        f"mode={current_session.mode} "
        f"deck_id={current_session.deck_id} "
        f"processed={current_session.position} "
        f"total={len(current_session.card_ids)} "
        f"scope={len(current_session.scope_card_ids)}"
    )

    session.clear_session()

    self.card = None
    self._v3 = None


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