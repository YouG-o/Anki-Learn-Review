from __future__ import annotations

import re
from typing import Any, Callable

from aqt import gui_hooks, mw
from aqt.deckbrowser import DeckBrowser

from .logger import log, log_debug, log_exception


BUTTON_STYLE = """
<style>
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
"""


GetTodayCounts = Callable[[int], tuple[int, int]]
StartSession = Callable[[str, int], None]

_get_today_counts: GetTodayCounts | None = None
_start_session: StartSession | None = None


def initialize(
    get_today_counts: GetTodayCounts,
    start_session: StartSession,
) -> None:
    """
    Initialize the Deck Browser integration.
    """
    global _get_today_counts
    global _start_session

    _get_today_counts = get_today_counts
    _start_session = start_session

    gui_hooks.deck_browser_will_render_content.append(
        on_deck_browser_will_render_content
    )

    gui_hooks.webview_did_receive_js_message.append(
        on_webview_did_receive_js_message
    )


def get_deck_button_html(
    deck_id: int,
    deck_name: str,
) -> str:
    """
    Create the dedicated Learn & Review column.

    Learn is always on the left.
    Review is always on the right.
    """
    try:
        if _get_today_counts is None:
            raise RuntimeError(
                "Deck Browser has not been initialized."
            )

        learn_count, review_count = _get_today_counts(
            deck_id
        )

        parts: list[str] = [
            '<td class="lr-column">',
            '<span class="lr-buttons" '
            'onclick="event.stopPropagation();">',
        ]

        # Learn - always left.
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

        parts.append("</span>")

        # Review - always right.
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

        parts.append("</span>")
        parts.append("</span>")
        parts.append("</td>")

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

        return '<td class="lr-column"></td>'


def add_learn_review_header(
    tree_html: str,
) -> str:
    """
    Add the Learn & Review header immediately before
    Anki's native Options column.
    """
    header_pattern = re.compile(
        r"""
        <th
        (?=[^>]*\bclass\s*=\s*["']?optscol["']?)
        [^>]*>
        .*?
        </th>
        """,
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )

    match = header_pattern.search(tree_html)

    if not match:
        log(
            "DECK_HEADER_OPTIONS_COLUMN_NOT_FOUND"
        )
        return tree_html

    if "lr-column-header" in tree_html:
        return tree_html

    existing_header = match.group(0)

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
    Adjust the native top-level drag row from 6 to 7 columns.
    """
    pattern = re.compile(
        r"""
        (<tr
        (?=[^>]*\bclass\s*=\s*["'][^"']*\btop-level-drag-row\b[^"']*["'])
        [^>]*>
        \s*<td
        (?=[^>]*\bcolspan\s*=\s*["']?)
        [^>]*?
        \bcolspan\s*=\s*["']?6["']?
        [^>]*>
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    match = pattern.search(tree_html)

    if not match:
        fallback = re.compile(
            r"""
            (<tr
            (?=[^>]*\bclass\s*=\s*["'][^"']*\btop-level-drag-row\b[^"']*["'])
            [^>]*>
            \s*<td
            [^>]*\bcolspan\s*=\s*["']?6["']?)
            """,
            re.IGNORECASE | re.VERBOSE,
        )

        fallback_match = fallback.search(tree_html)

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
    """
    row_pattern = re.compile(
        r"""
        <tr
        (?=[^>]*\bclass\s*=\s*["'][^"']*\bdeck\b[^"']*["'])
        (?=[^>]*\bid\s*=\s*["'](\d+)["'])
        [^>]*>
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    result: list[tuple[int, re.Match[str]]] = []

    for match in row_pattern.finditer(tree_html):
        try:
            deck_id = int(match.group(1))
            result.append((deck_id, match))
        except (TypeError, ValueError):
            continue

    return result


def inject_buttons_into_deck_row(
    tree_html: str,
    deck_id: int,
    deck_name: str,
) -> str:
    """
    Insert the Learn & Review cell immediately before
    Anki's native Options cell.
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
        re.IGNORECASE | re.DOTALL | re.VERBOSE,
    )

    match = row_pattern.search(tree_html)

    if not match:
        log(
            f"DECK_ROW_NOT_FOUND "
            f"deck_id={deck_id} "
            f"deck={deck_name!r}"
        )
        return tree_html

    row = match.group(0)

    if 'class="lr-column"' in row:
        return tree_html

    options_pattern = re.compile(
        r"""
        <td
        (?=[^>]*\bclass\s*=\s*["']?opts["']?)
        [^>]*>
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    options_match = options_pattern.search(row)

    if not options_match:
        log(
            f"OPTIONS_CELL_NOT_FOUND "
            f"deck_id={deck_id} "
            f"deck={deck_name!r}"
        )
        return tree_html

    insertion_point = options_match.start()

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
    """
    try:
        if mw.col is None:
            log(
                "DECK_RENDER skipped: no collection"
            )
            return

        log("DECK_RENDER start")

        tree = content.tree

        tree = add_learn_review_header(tree)

        tree = fix_top_level_drag_row_colspan(tree)

        rows = find_deck_rows(tree)

        log(
            f"DECK_RENDER rows={len(rows)}"
        )

        processed = 0
        buttons_added = 0

        for deck_id, _match in rows:
            try:
                deck = mw.col.decks.get(deck_id)

                if not deck:
                    log(
                        f"DECK_RENDER missing "
                        f"deck_id={deck_id}"
                    )
                    continue

                deck_name = str(deck["name"])

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

        content.tree = BUTTON_STYLE + tree

        log(
            f"DECK_RENDER complete "
            f"processed={processed} "
            f"dedicated_cells_added={buttons_added}"
        )

    except Exception:
        log_exception("DECK_RENDER_ERROR")


def handle_learn_review_command(
    message: str,
    context: Any,
) -> tuple[bool, Any] | None:
    """
    Handle:

        learn-review:learn:123
        learn-review:review:123
    """
    if not isinstance(context, DeckBrowser):
        return None

    prefix = "learn-review:"

    if not message.startswith(prefix):
        return None

    payload = message[len(prefix):]

    try:
        mode, deck_id_text = payload.split(":", 1)
        deck_id = int(deck_id_text)
    except Exception:
        log(
            f"COMMAND_INVALID "
            f"message={message!r}"
        )
        return True, None

    if mode not in ("learn", "review"):
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
        if _start_session is None:
            raise RuntimeError(
                "Deck Browser has not been initialized."
            )

        _start_session(
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
    result = handle_learn_review_command(
        message,
        context,
    )

    if result is None:
        return handled

    return result