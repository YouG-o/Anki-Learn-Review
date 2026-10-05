from __future__ import annotations

from aqt import gui_hooks, mw
from aqt.utils import tooltip

from .logger import ADDON_DIR, LOG_FILE, log, log_debug, log_exception

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


# ============================================================================
# Native Reviewer integration
# ============================================================================

from . import reviewer

compatibility_ok = reviewer.apply_patches()


# ============================================================================
# Session Manager & Deck Browser integration
# ============================================================================

from . import deck_browser, session_manager

if compatibility_ok:
    session_manager.register_hooks()
    deck_browser.initialize(
        start_session=session_manager.start_session,
    )
else:
    def on_incompatible_main_window_init() -> None:
        tooltip(
            "Learn & Review is incompatible with this Anki version.",
            period=5000,
        )

    gui_hooks.main_window_did_init.append(on_incompatible_main_window_init)


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

if compatibility_ok:
    log("Learn & Review - READY")
else:
    log("Learn & Review - ABORTED: Incompatible Anki version")