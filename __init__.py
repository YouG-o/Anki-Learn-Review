from __future__ import annotations

import logging
import re
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
from aqt.deckbrowser import DeckBrowser
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


@dataclass
class ReviewSession:
    """
    Session state.

    Learn:
        The initial card list is a strict one-pass worklist.
        Each New card is presented at most once during the session.

    Review:
        The initial card list defines the scope of the session:
        Learning + Relearning + Review cards that were available when
        the session started.

        The worklist is allowed to grow during the session, but only
        with cards that were already part of the initial scope.

        When a Review card is answered, the native scheduler is queried
        again. If that same card is immediately available according to
        Anki's scheduler, it is appended to the end of the worklist and
        can therefore be seen again during the same Review session.

        This reproduces the important native behavior where a card can
        require another pass during the same review session, while still
        preventing unrelated cards from entering the session.

    No deck limits or scheduler configuration are modified.
    """

    mode: str
    deck_id: int
    deck_name: str

    # Dynamic worklist.
    card_ids: list[int]

    # Immutable initial scope of the session.
    scope_card_ids: set[int]

    position: int = 0

    def remaining(self) -> int:
        return max(
            0,
            len(self.card_ids) - self.position,
        )

    def finished(self) -> bool:
        return self.position >= len(self.card_ids)

    def current_card_id(self) -> int | None:
        if self.finished():
            return None

        return self.card_ids[self.position]

    def advance(self) -> None:
        if not self.finished():
            self.position += 1

    def is_in_scope(self, card_id: int) -> bool:
        return int(card_id) in self.scope_card_ids

    def append_revisit(self, card_id: int) -> None:
        """
        Append a card for another pass.

        The caller is responsible for checking scheduler availability.
        """

        self.card_ids.append(
            int(card_id)
        )


SESSION: ReviewSession | None = None


# ============================================================================
# Scheduler helpers
# ============================================================================

def ensure_v3_scheduler() -> V3Scheduler:
    """
    Return the active V3 scheduler.

    Learn & Review relies on the native V3 scheduler to determine which
    cards are actually available today.
    """

    if mw.col is None:
        raise RuntimeError(
            "No Anki collection is open."
        )

    sched = mw.col.sched

    if not isinstance(sched, V3Scheduler):
        raise RuntimeError(
            "Learn & Review requires Anki's V3 scheduler."
        )

    return sched


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

    previous_deck_id = int(
        decks.get_current_id()
    )

    try:
        decks.select(deck_id)

        summary = sched.get_queued_cards(
            fetch_limit=1
        )

        total = (
            int(summary.new_count)
            + int(summary.learning_count)
            + int(summary.review_count)
        )

        log(
            f"SCHEDULER_QUEUE_SUMMARY "
            f"deck_id={deck_id} "
            f"new={summary.new_count} "
            f"learning={summary.learning_count} "
            f"review={summary.review_count} "
            f"total={total}"
        )

        if not fetch_all or total <= 0:
            return summary

        queue = sched.get_queued_cards(
            fetch_limit=total
        )

        log(
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
            log_exception(
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
                log(
                    f"CARD_CURRENTLY_AVAILABLE "
                    f"deck_id={deck_id} "
                    f"card_id={target_id} "
                    f"queue={queued_card_queue(qc)}"
                )

                return True

        log(
            f"CARD_NOT_CURRENTLY_AVAILABLE "
            f"deck_id={deck_id} "
            f"card_id={target_id}"
        )

        return False

    except Exception:
        log_exception(
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
            if queued_card_queue(qc)
            == int(QUEUE_TYPE_NEW)
        ]

        log(
            f"TODAY_CARDS "
            f"mode=learn "
            f"deck_id={deck_id} "
            f"count={len(result)} "
            f"snapshot=yes"
        )

        for index, card_id in enumerate(result):
            log(
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

        log(
            f"TODAY_CARDS "
            f"mode=review "
            f"deck_id={deck_id} "
            f"count={len(result)} "
            f"snapshot=yes"
        )

        for index, card_id in enumerate(result):
            log(
                f"SNAPSHOT_CARD "
                f"mode=review "
                f"index={index} "
                f"card_id={card_id}"
            )

        return result

    raise ValueError(
        f"Unknown session mode: {mode!r}"
    )


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

    learn_count = int(
        queue.new_count
    )

    review_count = (
        int(queue.learning_count)
        + int(queue.review_count)
    )

    log(
        f"TODAY_COUNTS "
        f"deck_id={deck_id} "
        f"learn={learn_count} "
        f"review={review_count}"
    )

    return (
        learn_count,
        review_count,
    )


# ============================================================================
# Deck Browser - dedicated Learn & Review column
# ============================================================================

BUTTON_STYLE = """
<style>
/*
 * Dedicated Learn & Review column.
 *
 * Toujours deux emplacements fixes :
 *
 *   [ Learn ] [ Review ]
 *
 * Même lorsqu'un seul bouton est présent.
 */

.lr-column-header {
    min-width: 125px;
    width: 125px;
    text-align: center;
    white-space: nowrap;
}

.lr-column {
    width: 125px;
    min-width: 125px;
    text-align: center;
    white-space: nowrap;
    padding-left: 4px;
    padding-right: 4px;
}

.lr-buttons {
    display: grid;
    grid-template-columns: 1fr 1fr;
    align-items: center;
    width: 100%;
    gap: 4px;
    white-space: nowrap;
}

.lr-button-slot {
    display: flex;
    justify-content: center;
    align-items: center;
    min-width: 0;
}

.lr-button {
    display: inline-block;
    width: 100%;
    box-sizing: border-box;
    padding: 2px 5px;
    border-radius: 4px;
    border: 1px solid rgba(127, 127, 127, 0.45);
    background: rgba(127, 127, 127, 0.10);
    color: inherit;
    text-decoration: none;
    cursor: pointer;
    font-size: 0.9em;
    text-align: center;
}

.lr-button:hover {
    background: rgba(127, 127, 127, 0.22);
}

.lr-button-learn {
    border-color: rgba(70, 130, 180, 0.55);
}

.lr-button-review {
    border-color: rgba(80, 150, 90, 0.55);
}
</style>
"""


def get_deck_button_html(
    deck_id: int,
    deck_name: str,
) -> str:
    """
    Create the dedicated Learn & Review column.

    The column always contains two fixed slots:

        [ Learn ] [ Review ]

    Learn is always on the left.
    Review is always on the right.

    If one button is unavailable, its slot remains empty so that
    the position of the other button never changes.
    """
    try:
        learn_count, review_count = get_today_counts(
            deck_id
        )

        parts: list[str] = [
            '<td class="lr-column">',
            '<span class="lr-buttons" '
            'onclick="event.stopPropagation();">'
        ]

        # ------------------------------------------------------------
        # Learn - ALWAYS LEFT
        # ------------------------------------------------------------
        parts.append(
            '<span class="lr-button-slot">'
        )

        if learn_count > 0:
            parts.append(
                f'<a class="lr-button lr-button-learn" '
                f'href="#" '
                f'onclick=\'return pycmd('
                f'"learn-review:learn:{deck_id}"'
                f');\'>'
                "Learn"
                "</a>"
            )

        parts.append(
            "</span>"
        )

        # ------------------------------------------------------------
        # Review - ALWAYS RIGHT
        # ------------------------------------------------------------
        parts.append(
            '<span class="lr-button-slot">'
        )

        if review_count > 0:
            parts.append(
                f'<a class="lr-button lr-button-review" '
                f'href="#" '
                f'onclick=\'return pycmd('
                f'"learn-review:review:{deck_id}"'
                f');\'>'
                "Review"
                "</a>"
            )

        parts.append(
            "</span>"
        )

        parts.append(
            "</span>"
        )

        parts.append(
            "</td>"
        )

        result = "".join(parts)

        log(
            f"DECK_BUTTONS "
            f"deck_id={deck_id} "
            f"deck={deck_name!r} "
            f"learn={learn_count} "
            f"review={review_count}"
        )

        return result

    except Exception:
        log_exception(
            f"DECK_BUTTONS_ERROR "
            f"deck_id={deck_id} "
            f"deck={deck_name!r}"
        )

        # Même en cas d'erreur, on conserve la colonne dédiée.
        return '<td class="lr-column"></td>'


# ============================================================================
# HTML helpers
# ============================================================================

def add_learn_review_header(
    tree_html: str,
) -> str:
    """
    Add the dedicated Learn & Review header immediately before Anki's
    native Options column.

    Native 26.08 generates:

        <th class=optscol></th>

    We transform it into:

        <th class=lr-column-header>Learn & Review</th>
        <th class=optscol></th>

    This keeps the gear in its own native column.
    """

    header_pattern = re.compile(
        r"""
        <th
        (?=[^>]*\bclass\s*=\s*["']?optscol["']?)
        [^>]*>
        .*?
        </th>
        """,
        re.IGNORECASE
        | re.DOTALL
        | re.VERBOSE,
    )

    match = header_pattern.search(
        tree_html
    )

    if not match:
        log(
            "DECK_HEADER_OPTIONS_COLUMN_NOT_FOUND"
        )

        return tree_html

    existing_header = match.group(0)

    # Avoid duplicate insertion if another hook causes this function
    # to run more than once on the same HTML.
    if "lr-column-header" in tree_html:
        return tree_html

    new_header = (
        '<th class="lr-column-header">'
        "Learn &amp; Review"
        "</th>"
        + existing_header
    )

    return (
        tree_html[:match.start()]
        + new_header
        + tree_html[match.end():]
    )


def fix_top_level_drag_row_colspan(
    tree_html: str,
) -> str:
    """
    The native top-level drag row spans 6 columns.

    After adding our dedicated column, it needs to span 7 columns
    so that the three counters + our column + options column remain
    aligned correctly.
    """

    pattern = re.compile(
        r"""
        (<tr
        (?=[^>]*\bclass\s*=\s*["'][^"']*\btop-level-drag-row\b[^"']*["'])
        [^>]*>
        \s*<td
        (?=[^>]*\bcolspan\s*=\s*["']?)([^>]*?)
        \bcolspan\s*=\s*["']?6["']?
        ([^>]*)
        >
        )
        """,
        re.IGNORECASE
        | re.VERBOSE,
    )

    match = pattern.search(
        tree_html
    )

    if not match:
        # Fall back to a simpler direct replacement if Anki changes
        # the exact formatting of this row.
        fallback = re.compile(
            r"""
            (<tr
            (?=[^>]*\bclass\s*=\s*["'][^"']*\btop-level-drag-row\b[^"']*["'])
            [^>]*>
            \s*<td
            [^>]*\bcolspan\s*=\s*["']?6["']?
            )
            """,
            re.IGNORECASE
            | re.VERBOSE,
        )

        fallback_match = fallback.search(
            tree_html
        )

        if not fallback_match:
            log(
                "TOP_LEVEL_DRAG_ROW_NOT_FOUND"
            )

            return tree_html

        return (
            tree_html[:fallback_match.start(1)]
            + fallback_match.group(1).replace(
                "colspan=6",
                "colspan=7",
            )
            + tree_html[fallback_match.end(1):]
        )

    return (
        tree_html[:match.start()]
        + match.group(1).replace(
            "6",
            "7",
            1,
        )
        + tree_html[match.end():]
    )


def find_deck_rows(
    tree_html: str,
) -> list[tuple[int, re.Match[str]]]:
    """
    Find native Deck Browser rows.

    Anki 26.08.1 generates HTML such as:

        <tr class = 'deck' id = '1790495196096'>

    The regex therefore allows arbitrary whitespace around '='.
    """

    row_pattern = re.compile(
        r"""
        <tr
        (?=[^>]*\bclass\s*=\s*["'][^"']*\bdeck\b[^"']*["'])
        (?=[^>]*\bid\s*=\s*["'](\d+)["'])
        [^>]*>
        """,
        re.IGNORECASE
        | re.VERBOSE,
    )

    result: list[tuple[int, re.Match[str]]] = []

    for match in row_pattern.finditer(tree_html):
        try:
            deck_id = int(
                match.group(1)
            )

            result.append(
                (deck_id, match)
            )

        except (TypeError, ValueError):
            continue

    return result


def inject_buttons_into_deck_row(
    tree_html: str,
    deck_id: int,
    deck_name: str,
) -> str:
    """
    Insert our dedicated Learn & Review cell immediately BEFORE the
    native Options cell.

    We deliberately do NOT insert anything inside the native opts cell.
    """

    buttons = get_deck_button_html(
        deck_id,
        deck_name,
    )

    row_pattern = re.compile(
        rf"""
        <tr
        (?=[^>]*\bclass\s*=\s*["'][^"']*\bdeck\b[^"']*["'])
        (?=[^>]*\bid\s*=\s*["']{re.escape(str(deck_id))}["'])
        [^>]*>
        .*?
        </tr>
        """,
        re.IGNORECASE
        | re.DOTALL
        | re.VERBOSE,
    )

    match = row_pattern.search(
        tree_html
    )

    if not match:
        log(
            f"DECK_ROW_NOT_FOUND "
            f"deck_id={deck_id} "
            f"deck={deck_name!r}"
        )

        return tree_html

    row = match.group(0)

    # Prevent duplicate injection.
    if "class=\"lr-column\"" in row:
        return tree_html

    # Find the native Options cell.
    options_pattern = re.compile(
        r"""
        <td
        (?=[^>]*\bclass\s*=\s*["']?opts["']?)
        [^>]*>
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    options_match = options_pattern.search(
        row
    )

    if not options_match:
        log(
            f"OPTIONS_CELL_NOT_FOUND "
            f"deck_id={deck_id} "
            f"deck={deck_name!r}"
        )

        return tree_html

    # Insert BEFORE the native options cell.
    insertion_point = (
        options_match.start()
    )

    updated_row = (
        row[:insertion_point]
        + buttons
        + row[insertion_point:]
    )

    return (
        tree_html[:match.start()]
        + updated_row
        + tree_html[match.end():]
    )


def on_deck_browser_will_render_content(
    deck_browser: DeckBrowser,
    content: Any,
) -> None:
    """
    Modify the already-rendered native Deck Browser HTML.

    Our dedicated column is structurally independent from Anki's
    native Options column.
    """

    try:
        if mw.col is None:
            log(
                "DECK_RENDER skipped: no collection"
            )
            return

        log(
            "DECK_RENDER start"
        )

        tree = content.tree

        # ----------------------------------------------------------------
        # Header
        # ----------------------------------------------------------------

        tree = add_learn_review_header(
            tree
        )

        # ----------------------------------------------------------------
        # Top-level drag row
        # ----------------------------------------------------------------

        tree = fix_top_level_drag_row_colspan(
            tree
        )

        # ----------------------------------------------------------------
        # Deck rows
        # ----------------------------------------------------------------

        rows = find_deck_rows(
            tree
        )

        log(
            f"DECK_RENDER rows={len(rows)}"
        )

        processed = 0
        buttons_added = 0

        for deck_id, _match in rows:
            try:
                deck = mw.col.decks.get(
                    deck_id
                )

                if not deck:
                    log(
                        f"DECK_RENDER missing "
                        f"deck_id={deck_id}"
                    )

                    continue

                deck_name = str(
                    deck["name"]
                )

                old_tree = tree

                tree = inject_buttons_into_deck_row(
                    tree,
                    deck_id,
                    deck_name,
                )

                processed += 1

                if tree != old_tree:
                    buttons_added += 1

            except Exception:
                log_exception(
                    f"DECK_RENDER_ROW_ERROR "
                    f"deck_id={deck_id}"
                )

        content.tree = (
            BUTTON_STYLE
            + tree
        )

        log(
            f"DECK_RENDER complete "
            f"processed={processed} "
            f"dedicated_cells_added={buttons_added}"
        )

    except Exception:
        log_exception(
            "DECK_RENDER_ERROR"
        )


gui_hooks.deck_browser_will_render_content.append(
    on_deck_browser_will_render_content
)


# ============================================================================
# JS command handling
# ============================================================================

def handle_learn_review_command(
    message: str,
    context: Any,
) -> tuple[bool, Any] | None:
    """
    Handle:

        learn-review:learn:123
        learn-review:review:123
    """

    if not isinstance(
        context,
        DeckBrowser,
    ):
        return None

    prefix = "learn-review:"

    if not message.startswith(
        prefix
    ):
        return None

    payload = message[
        len(prefix):
    ]

    try:
        mode, deck_id_text = (
            payload.split(":", 1)
        )

        deck_id = int(
            deck_id_text
        )

    except Exception:
        log(
            f"COMMAND_INVALID "
            f"message={message!r}"
        )

        return True, None

    if mode not in (
        "learn",
        "review",
    ):
        log(
            f"COMMAND_INVALID_MODE "
            f"mode={mode!r}"
        )

        return True, None

    log(
        f"COMMAND_RECEIVED "
        f"mode={mode} "
        f"deck_id={deck_id}"
    )

    try:
        start_session(
            mode,
            deck_id,
        )

    except Exception:
        log_exception(
            f"START_SESSION_ERROR "
            f"mode={mode} "
            f"deck_id={deck_id}"
        )

        return True, None

    return True, None


def on_webview_did_receive_js_message(
    handled: tuple[bool, Any],
    message: str,
    context: Any,
) -> tuple[bool, Any]:

    result = (
        handle_learn_review_command(
            message,
            context,
        )
    )

    if result is None:
        return handled

    return result


gui_hooks.webview_did_receive_js_message.append(
    on_webview_did_receive_js_message
)


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

    states = (
        mw.col._backend.get_scheduling_states(
            card.id
        )
    )

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

    global SESSION

    session = SESSION

    if session is None:
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
        f"mode={session.mode} "
        f"card_id={card.id} "
        f"ease={ease} "
        f"position={session.position} "
        f"remaining_before={session.remaining()}"
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
        sched = ensure_v3_scheduler()

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
            f"mode={session.mode} "
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

    global SESSION

    session = SESSION

    if session is None:
        return ORIGINAL_GET_NEXT_V3_CARD(
            self
        )

    log(
        f"REVIEWER_NEXT "
        f"mode={session.mode} "
        f"deck_id={session.deck_id} "
        f"position={session.position} "
        f"total={len(session.card_ids)} "
        f"remaining={session.remaining()} "
        f"scope={len(session.scope_card_ids)}"
    )

    while not session.finished():

        card_id = (
            session.current_card_id()
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

                session.advance()
                continue

            if not card_matches_session(
                card,
                session.mode,
            ):
                log(
                    f"SESSION_CARD_SKIPPED "
                    f"card_id={card_id} "
                    f"mode={session.mode} "
                    f"type={card.type} "
                    f"queue={card.queue}"
                )

                session.advance()
                continue

            states_info = (
                get_custom_scheduling_info(
                    self,
                    card,
                    session,
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
                f"position={session.position} "
                f"remaining={session.remaining()}"
            )

            return

        except Exception:
            log_exception(
                f"SESSION_CARD_LOAD_ERROR "
                f"card_id={card_id}"
            )

            session.advance()

    log(
        f"SESSION_FINISHED "
        f"mode={session.mode} "
        f"deck_id={session.deck_id} "
        f"processed={session.position} "
        f"total={len(session.card_ids)} "
        f"scope={len(session.scope_card_ids)}"
    )

    SESSION = None

    self.card = None
    self._v3 = None


# ============================================================================
# Start a session
# ============================================================================

def start_session(
    mode: str,
    deck_id: int,
) -> None:

    global SESSION

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

    card_ids = get_today_card_ids(
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

    SESSION = ReviewSession(
        mode=mode,
        deck_id=deck_id,
        deck_name=deck_name,
        card_ids=list(card_ids),
        scope_card_ids=set(card_ids),
        position=0,
    )

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
        SESSION = None

        log_exception(
            "MOVE_TO_REVIEW_ERROR"
        )

        raise


# ============================================================================
# Synchronize session after answering
# ============================================================================

def on_reviewer_did_answer_card(
    reviewer: Reviewer,
    card: Card,
    ease: int,
) -> None:

    global SESSION

    if SESSION is None:
        return

    try:
        session = SESSION

        log(
            f"ANSWERED "
            f"card_id={card.id} "
            f"ease={ease} "
            f"mode={session.mode} "
            f"session_position={session.position} "
            f"session_remaining_before={session.remaining()}"
        )

        current_id = (
            session.current_card_id()
        )

        if current_id != card.id:
            log(
                f"SESSION_POSITION_MISMATCH "
                f"expected={current_id} "
                f"answered={card.id}"
            )

            return

        session.advance()

        log(
            f"SESSION_ADVANCE "
            f"answered_card={card.id} "
            f"new_position={session.position} "
            f"remaining={session.remaining()}"
        )

        # ----------------------------------------------------------------
        # Learn
        # ----------------------------------------------------------------

        if session.mode == "learn":
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

        if session.mode == "review":

            if not session.is_in_scope(
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
                is_card_currently_queued(
                    session.deck_id,
                    int(card.id),
                )
            )

            if currently_available:
                session.append_revisit(
                    int(card.id)
                )

                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=yes "
                    f"reason=native_scheduler_available "
                    f"new_total={len(session.card_ids)} "
                    f"new_remaining={session.remaining()}"
                )

            else:
                log(
                    f"SESSION_REINSERT "
                    f"mode=review "
                    f"card_id={card.id} "
                    f"reinserted=no "
                    f"reason=native_scheduler_not_available "
                    f"new_remaining={session.remaining()}"
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

    global SESSION

    if (
        old_state == "review"
        and new_state != "review"
    ):
        if SESSION is not None:
            log(
                f"SESSION_ABORTED "
                f"state_change "
                f"old={old_state!r} "
                f"new={new_state!r} "
                f"position={SESSION.position} "
                f"total={len(SESSION.card_ids)} "
                f"scope={len(SESSION.scope_card_ids)}"
            )

            SESSION = None


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
    "Deck Browser hook installed"
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