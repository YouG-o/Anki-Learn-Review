from __future__ import annotations

from typing import Any, Callable

from anki.consts import (
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
)
from anki.scheduler.v3 import Scheduler as V3Scheduler
from aqt import mw

Log = Callable[[str], None]
LogException = Callable[[str], None]

_log: Log = lambda _message: None
_log_exception: LogException = lambda _message: None


def init_logger(log: Log, log_exception: LogException) -> None:
    """
    Configure logging callbacks for the scheduler integration.
    """
    global _log, _log_exception
    _log = log
    _log_exception = log_exception


def ensure_v3_scheduler() -> V3Scheduler:
    """
    Return the active V3 scheduler.

    Learn & Review relies on the native V3 scheduler to determine which
    cards are actually available today.
    """
    if mw.col is None:
        raise RuntimeError("No Anki collection is open.")

    sched = mw.col.sched

    if not isinstance(sched, V3Scheduler):
        raise RuntimeError("Learn & Review requires Anki's V3 scheduler.")

    return sched


def get_backend_scheduling_states(card_id: int) -> Any:
    """
    Retrieve scheduling states for a card directly from the backend.

    NOTE: Direct call to mw.col._backend is a private API and a known
    fragility point against future Anki internal changes.
    """
    if mw.col is None:
        raise RuntimeError("No Anki collection is open.")

    return mw.col._backend.get_scheduling_states(card_id)


def get_scheduler_queue(
    deck_id: int,
    fetch_all: bool = True,
) -> Any:
    """
    Read Anki's native V3 scheduler queue for the selected deck.

    This only reads scheduler state.

    It does NOT:
        - modify deck configuration
        - modify daily limits
        - create a filtered deck
        - alter scheduler settings
    """
    sched = ensure_v3_scheduler()

    decks = mw.col.decks

    previous_deck_id = int(decks.get_current_id())

    try:
        decks.select(deck_id)

        summary = sched.get_queued_cards(fetch_limit=1)

        total = (
            int(summary.new_count)
            + int(summary.learning_count)
            + int(summary.review_count)
        )

        _log(
            f"SCHEDULER_QUEUE_SUMMARY "
            f"deck_id={deck_id} "
            f"new={summary.new_count} "
            f"learning={summary.learning_count} "
            f"review={summary.review_count} "
            f"total={total}"
        )

        if not fetch_all or total <= 0:
            return summary

        queue = sched.get_queued_cards(fetch_limit=total)

        _log(
            f"SCHEDULER_QUEUE_FETCHED "
            f"deck_id={deck_id} "
            f"cards={len(queue.cards)} "
            f"new={queue.new_count} "
            f"learning={queue.learning_count} "
            f"review={queue.review_count}"
        )

        return queue

    finally:
        try:
            decks.select(previous_deck_id)
        except Exception:
            _log_exception(
                "SCHEDULER_QUEUE_RESTORE_DECK_ERROR "
                f"previous_deck_id={previous_deck_id}"
            )


def queued_card_id(qc: Any) -> int:
    return int(qc.card.id)


def queued_card_queue(qc: Any) -> int:
    return int(qc.queue)


def is_card_currently_queued(
    deck_id: int,
    card_id: int,
) -> bool:
    """
    Ask the native V3 scheduler whether a specific card is currently
    present in its available queue.
    """
    try:
        queue = get_scheduler_queue(
            deck_id,
            fetch_all=True,
        )

        target_id = int(card_id)

        for qc in queue.cards:
            if queued_card_id(qc) == target_id:
                _log(
                    f"CARD_CURRENTLY_AVAILABLE "
                    f"deck_id={deck_id} "
                    f"card_id={target_id} "
                    f"queue={queued_card_queue(qc)}"
                )

                return True

        _log(
            f"CARD_NOT_CURRENTLY_AVAILABLE "
            f"deck_id={deck_id} "
            f"card_id={target_id}"
        )

        return False

    except Exception:
        _log_exception(
            f"CARD_AVAILABILITY_CHECK_ERROR "
            f"deck_id={deck_id} "
            f"card_id={card_id}"
        )

        return False


def get_today_card_ids(
    mode: str,
    deck_id: int,
) -> list[int]:
    """
    Create the initial session scope from Anki's native scheduler queue.

    Learn:
        New cards only.

    Review:
        Learning + Review cards.

    In Anki's queued-card abstraction, Relearning is represented by the
    Learning queue. Therefore it is included in Review automatically.
    """
    queue = get_scheduler_queue(
        deck_id,
        fetch_all=True,
    )

    if mode == "learn":
        result = [
            queued_card_id(qc)
            for qc in queue.cards
            if queued_card_queue(qc) == int(QUEUE_TYPE_NEW)
        ]

        _log(
            f"TODAY_CARDS "
            f"mode=learn "
            f"deck_id={deck_id} "
            f"count={len(result)} "
            f"snapshot=yes"
        )

        for index, card_id in enumerate(result):
            _log(
                f"SNAPSHOT_CARD "
                f"mode=learn "
                f"index={index} "
                f"card_id={card_id}"
            )

        return result

    if mode == "review":
        result = [
            queued_card_id(qc)
            for qc in queue.cards
            if queued_card_queue(qc)
            in {
                int(QUEUE_TYPE_LRN),
                int(QUEUE_TYPE_REV),
            }
        ]

        _log(
            f"TODAY_CARDS "
            f"mode=review "
            f"deck_id={deck_id} "
            f"count={len(result)} "
            f"snapshot=yes"
        )

        for index, card_id in enumerate(result):
            _log(
                f"SNAPSHOT_CARD "
                f"mode=review "
                f"index={index} "
                f"card_id={card_id}"
            )

        return result

    raise ValueError(f"Unknown session mode: {mode!r}")


def get_today_counts(
    deck_id: int,
) -> tuple[int, int]:
    """
    Return:

        new_available_today
        review_available_today

    based on the native V3 scheduler queue.

    Review means:
        Learning + Review + Relearning
    """
    queue = get_scheduler_queue(
        deck_id,
        fetch_all=False,
    )

    learn_count = int(queue.new_count)

    review_count = int(queue.learning_count) + int(queue.review_count)

    _log(
        f"TODAY_COUNTS "
        f"deck_id={deck_id} "
        f"learn={learn_count} "
        f"review={review_count}"
    )

    return (
        learn_count,
        review_count,
    )
