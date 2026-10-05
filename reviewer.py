from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anki.cards import Card
from anki.consts import (
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
)
from anki.scheduler_pb2 import CardAnswer
from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer

from . import scheduler, session
from .logger import log, log_debug, log_exception
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


ORIGINAL_GET_NEXT_V3_CARD = Reviewer._get_next_v3_card
ORIGINAL_ANSWER_CARD = Reviewer._answerCard


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
        states.current.custom_data = card.custom_data
    except Exception:
        log(
            "STATE_CUSTOM_DATA_ASSIGN_FAILED "
            f"card_id={card.id}"
        )

    remaining = session.remaining()

    if session.mode == "learn":
        return SessionV3Info(
            states=states,
            context=SessionContext(deck_name=session.deck_name),
            remaining_new=remaining,
            remaining_learning=0,
            remaining_review=0,
            mode="learn",
        )

    return SessionV3Info(
        states=states,
        context=SessionContext(deck_name=session.deck_name),
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
            and card.queue == int(QUEUE_TYPE_NEW)
        )

    if mode == "review":
        return card.queue in {
            int(QUEUE_TYPE_LRN),
            int(QUEUE_TYPE_REV),
            int(QUEUE_TYPE_DAY_LEARN_RELEARN),
        }

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

    NOTE: Direct call to mw.col._backend is a private API and a known
    fragility point against future Anki internal changes.
    """
    rating_map = {
        1: CardAnswer.AGAIN,
        2: CardAnswer.HARD,
        3: CardAnswer.GOOD,
        4: CardAnswer.EASY,
    }

    try:
        rating = rating_map[ease]
    except KeyError:
        raise ValueError(f"Invalid ease: {ease}")

    log(
        f"GRADE_NOW "
        f"card_id={card.id} "
        f"ease={ease} "
        f"rating={int(rating)}"
    )

    if mw.col is None:
        raise RuntimeError("No Anki collection is open.")

    changes = mw.col._backend.grade_now(
        card_ids=[int(card.id)],
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
        log("ANSWER_ABORTED no current card")
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

    proceed, ease = gui_hooks.reviewer_will_answer_card(
        (True, ease),
        self,
        card,
    )

    if not proceed:
        log(
            f"SESSION_ANSWER_CANCELLED "
            f"card_id={card.id}"
        )
        return

    try:
        sched = scheduler.ensure_v3_scheduler()

        rating = SessionV3Info.rating_from_ease(ease)

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
            raise ValueError(f"Unsupported rating: {rating}")

        is_leech = sched.state_is_leech(new_state)

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

        self._after_answering(ease)

        if is_leech:
            self.onLeech(suspended)

    except Exception:
        log_exception(
            f"SESSION_GRADE_NOW_ERROR "
            f"card_id={card.id} "
            f"ease={ease}"
        )
        self.state = "answer"


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
        return ORIGINAL_GET_NEXT_V3_CARD(self)

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
        card_id = current_session.current_card_id()

        if card_id is None:
            break

        try:
            if mw.col is None:
                raise RuntimeError("No Anki collection is open.")

            card = mw.col.get_card(card_id)

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

            states_info = get_custom_scheduling_info(
                self,
                card,
                current_session,
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


_patches_applied: bool = False


def is_compatible() -> bool:
    """
    Check if the native Reviewer has the methods expected by Learn & Review.
    """
    return hasattr(Reviewer, "_get_next_v3_card") and hasattr(
        Reviewer, "_answerCard"
    )


def apply_patches() -> bool:
    """
    Install Reviewer monkey patches safely.
    Returns True if patches were installed or already installed, False otherwise.
    """
    global _patches_applied

    if _patches_applied:
        return True

    if not is_compatible():
        log("REVIEWER_PATCH_ERROR: expected Reviewer methods missing")
        return False

    Reviewer._get_next_v3_card = lambda self: custom_get_next_v3_card(self)
    Reviewer._answerCard = custom_answer_card
    _patches_applied = True
    log("Reviewer patches successfully installed")
    return True
