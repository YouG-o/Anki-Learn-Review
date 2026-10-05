from __future__ import annotations

from anki.cards import Card
from aqt import gui_hooks, mw
from aqt.reviewer import Reviewer

from . import scheduler, session
from .logger import log, log_debug, log_exception
from .session import ReviewSession


def start_session(
    mode: str,
    deck_id: int,
) -> None:
    """
    Start a Learn or Review session for the given deck.
    """
    if mw.col is None:
        raise RuntimeError("No Anki collection is open.")

    if mode not in (
        "learn",
        "review",
    ):
        raise ValueError(f"Invalid session mode: {mode!r}")

    deck = mw.col.decks.get(deck_id)

    if not deck:
        raise RuntimeError(f"Deck not found: {deck_id}")

    deck_name = str(deck["name"])

    log(
        f"START_SESSION "
        f"mode={mode} "
        f"deck_id={deck_id} "
        f"deck_name={deck_name!r}"
    )

    mw.col.decks.select(deck_id)

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
        mw.moveToState("review")
        log("MOVE_TO_REVIEW complete")
    except Exception:
        session.clear_session()
        log_exception("MOVE_TO_REVIEW_ERROR")
        raise


def on_reviewer_did_answer_card(
    reviewer: Reviewer,
    card: Card,
    ease: int,
) -> None:
    """
    Handle session worklist synchronization after a card has been answered.
    """
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

        current_id = active_session.current_card_id()

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
            if not active_session.is_in_scope(card.id):
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

            currently_available = scheduler.is_card_currently_queued(
                active_session.deck_id,
                int(card.id),
            )

            if currently_available:
                active_session.append_revisit(int(card.id))
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


def on_state_will_change(
    new_state: str,
    old_state: str,
) -> None:
    """
    Abort the custom session if navigating away from the Review state.
    """
    if old_state == "review" and new_state != "review":
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


def register_hooks() -> None:
    """
    Register session lifecycle hooks.
    """
    gui_hooks.reviewer_did_answer_card.append(on_reviewer_did_answer_card)
    gui_hooks.state_will_change.append(on_state_will_change)
