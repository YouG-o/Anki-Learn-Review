from __future__ import annotations

from dataclasses import dataclass


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


def get_session() -> ReviewSession | None:
    return SESSION


def set_session(session: ReviewSession | None) -> None:
    global SESSION
    SESSION = session


def clear_session() -> None:
    global SESSION
    SESSION = None
