from __future__ import annotations

from anki.cards import Card
from anki.template import TemplateRenderContext

from . import presentation_config
from .logger import log, log_exception
from .session import ReviewSession


def apply_session_presentation(card: Card, active_session: ReviewSession) -> None:
    """Render the alternative in memory, leaving the note and scheduler untouched.

    Using Anki's template renderer (instead of interpolating {{fields}}) preserves
    filters, conditional sections, FrontSide, CSS, and automatic audio parsing.
    set_render_output() ensures the Reviewer sees the same content/audio on both
    sides, even when it asks the card for question/answer AV tags separately.
    """
    try:
        note = card.note()
        model = card.note_type()
        if model is None:
            return

        alternative = presentation_config.alternate_for(
            active_session.deck_id, active_session.mode, int(note.mid)
        )
        if alternative is None:
            return

        template = dict(card.template())
        template["qfmt"] = alternative["front"]
        template["afmt"] = alternative["back"]

        rendered = TemplateRenderContext.from_card_layout(
            note=note,
            card=card,
            notetype=model,
            template=template,
            fill_empty=False,
        ).render()
        extra_css = alternative.get("css", "")
        if extra_css:
            rendered.css += "\n" + extra_css
        card.set_render_output(rendered)

        log(
            f"PRESENTATION_APPLIED "
            f"deck_id={active_session.deck_id} "
            f"model_id={note.mid} "
            f"mode={active_session.mode} "
            f"card_id={card.id}"
        )
    except Exception:
        # Rendering errors must never prevent a scheduled card from appearing.
        log_exception(f"PRESENTATION_FALLBACK card_id={card.id}")
