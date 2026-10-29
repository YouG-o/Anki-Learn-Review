from __future__ import annotations

from copy import deepcopy

from aqt import gui_hooks, mw
from aqt.qt import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    qconnect,
)

from . import presentation_config
from .logger import log_exception


MODE_LABELS = (
    (presentation_config.ORIGINAL, "Original (Anki)"),
    (presentation_config.ALTERNATIVE, "Alternative"),
)


def note_types_in_deck(deck_id: int) -> list[tuple[int, str]]:
    """Find note types in this deck AND its descendants, without changing it."""
    if mw.col is None:
        return []

    deck = mw.col.decks.get(deck_id)
    if deck is None:
        return []

    root = str(deck["name"])
    deck_ids = [
        int(item.id)
        for item in mw.col.decks.all_names_and_ids()
        if item.name == root or item.name.startswith(root + "::")
    ]
    if not deck_ids:
        return []

    placeholders = ",".join("?" for _ in deck_ids)
    model_ids = mw.col.db.list(
        "SELECT DISTINCT n.mid FROM notes n "
        "JOIN cards c ON c.nid = n.id "
        f"WHERE c.did IN ({placeholders}) ORDER BY n.mid",
        *deck_ids,
    )
    models = [mw.col.models.get(int(model_id)) for model_id in model_ids]
    return sorted(
        [(int(model["id"]), str(model["name"])) for model in models if model],
        key=lambda item: item[1].casefold(),
    )


class DeckSettingsDialog(QDialog):
    """Deck-scoped editor. All modifications remain drafts until Save."""

    def __init__(self, deck_id: int) -> None:
        super().__init__(mw)
        if mw.col is None:
            raise RuntimeError("No Anki collection is open")
        deck = mw.col.decks.get(deck_id)
        if deck is None:
            raise RuntimeError(f"Deck not found: {deck_id}")

        self.deck_id = deck_id
        self._settings = presentation_config.get_deck_settings(deck_id)
        self._alternatives = deepcopy(self._settings["alternatives"])
        self._types = note_types_in_deck(deck_id)
        self._active_type_id: str | None = None
        self._loading = False

        self.setWindowTitle(f"Learn & Review — {deck['name']}")
        self.resize(880, 700)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Choose which presentation to use for each session. "
            "By default, Anki uses the original card template. "
            "Alternatives are specific to this deck and do not create new cards."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        modes = QGroupBox("Presentation by session")
        mode_form = QFormLayout(modes)
        self.learn_choice = self._mode_combo(self._settings["learn"])
        self.review_choice = self._mode_combo(self._settings["review"])
        mode_form.addRow("Learn :", self.learn_choice)
        mode_form.addRow("Review :", self.review_choice)
        layout.addWidget(modes)

        alternative_group = QGroupBox("Create / edit alternative presentation")
        alternative_layout = QVBoxLayout(alternative_group)
        info = QLabel(
            "An alternative presentation has HTML front and back templates "
            "for each note type. Use Anki fields (e.g. {{French}}) and "
            "{{FrontSide}}. The original CSS is still applied."
        )
        info.setWordWrap(True)
        alternative_layout.addWidget(info)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Note type:"))
        self.note_type_combo = QComboBox()
        for mid, name in self._types:
            self.note_type_combo.addItem(name, str(mid))
        type_row.addWidget(self.note_type_combo, stretch=1)
        alternative_layout.addLayout(type_row)

        button_row = QHBoxLayout()
        self.create_button = QPushButton("Copy from Anki template")
        self.delete_button = QPushButton("Delete alternative")
        button_row.addWidget(self.create_button)
        button_row.addWidget(self.delete_button)
        button_row.addStretch()
        alternative_layout.addLayout(button_row)

        alternative_layout.addWidget(QLabel("Front (HTML):"))
        self.front_edit = QPlainTextEdit()
        alternative_layout.addWidget(self.front_edit, stretch=1)
        alternative_layout.addWidget(QLabel("Back (HTML):"))
        self.back_edit = QPlainTextEdit()
        alternative_layout.addWidget(self.back_edit, stretch=1)
        alternative_layout.addWidget(QLabel("Additional CSS (optional):"))
        self.css_edit = QPlainTextEdit()
        alternative_layout.addWidget(self.css_edit, stretch=1)
        layout.addWidget(alternative_group, stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(self.buttons)

        qconnect(self.note_type_combo.currentIndexChanged, self._on_type_changed)
        qconnect(self.create_button.clicked, self._create_alternative)
        qconnect(self.delete_button.clicked, self._delete_alternative)
        qconnect(self.buttons.accepted, self._save)
        qconnect(self.buttons.rejected, self.reject)
        self._on_type_changed()

    @staticmethod
    def _mode_combo(selected: str) -> QComboBox:
        combo = QComboBox()
        for value, label in MODE_LABELS:
            combo.addItem(label, value)
        index = combo.findData(selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    def _capture_edits(self) -> None:
        if self._loading or self._active_type_id is None:
            return
        if self._active_type_id in self._alternatives:
            self._alternatives[self._active_type_id] = {
                "front": self.front_edit.toPlainText(),
                "back": self.back_edit.toPlainText(),
                "css": self.css_edit.toPlainText(),
            }

    def _on_type_changed(self, *_args: object) -> None:
        self._capture_edits()
        self._active_type_id = self.note_type_combo.currentData()
        alternative = self._alternatives.get(self._active_type_id)
        available = alternative is not None
        self._loading = True
        self.front_edit.setPlainText(alternative["front"] if available else "")
        self.back_edit.setPlainText(alternative["back"] if available else "")
        self.css_edit.setPlainText(alternative.get("css", "") if available else "")
        self.front_edit.setEnabled(available)
        self.back_edit.setEnabled(available)
        self.css_edit.setEnabled(available)
        self.create_button.setEnabled(self._active_type_id is not None and not available)
        self.delete_button.setEnabled(available)
        self._loading = False

    def _create_alternative(self, *_args: object) -> None:
        model_id = self._active_type_id
        if model_id is None or mw.col is None:
            return
        model = mw.col.models.get(int(model_id))
        if not model or not model.get("tmpls"):
            return
        original = model["tmpls"][0]
        self._alternatives[model_id] = {
            "front": original["qfmt"],
            "back": original["afmt"],
            "css": "",
        }
        self._loading = True  # Do not overwrite the fresh copy with empty editors.
        self._on_type_changed()

    def _delete_alternative(self, *_args: object) -> None:
        if self._active_type_id is not None:
            self._alternatives.pop(self._active_type_id, None)
            if not self._alternatives:
                self.learn_choice.setCurrentIndex(0)
                self.review_choice.setCurrentIndex(0)
            self._on_type_changed()

    def _save(self) -> None:
        self._capture_edits()
        for model_id, template in self._alternatives.items():
            if not template["front"].strip() or not template["back"].strip():
                QMessageBox.warning(
                    self, "Learn & Review",
                    "Each alternative presentation must have non-empty "
                    "front and back templates."
                )
                return

        learn = self.learn_choice.currentData()
        review = self.review_choice.currentData()
        if (learn == presentation_config.ALTERNATIVE or review == presentation_config.ALTERNATIVE) and not self._alternatives:
            QMessageBox.warning(
                self, "Learn & Review",
                "Create an alternative presentation for at least one note type first."
            )
            return

        try:
            presentation_config.save_deck_settings(
                self.deck_id,
                {"learn": learn, "review": review, "alternatives": self._alternatives},
            )
        except Exception:
            log_exception(f"DECK_SETTINGS_SAVE_ERROR deck_id={self.deck_id}")
            QMessageBox.critical(self, "Learn & Review", "Could not save deck settings.")
            return
        self.accept()


def show_deck_settings(deck_id: int) -> None:
    try:
        DeckSettingsDialog(int(deck_id)).exec()
    except Exception:
        log_exception(f"DECK_SETTINGS_OPEN_ERROR deck_id={deck_id}")
        QMessageBox.critical(mw, "Learn & Review", "Could not open deck settings.")


def on_deck_options_menu(menu: object, deck_id: int) -> None:
    """Use the existing native gear menu, without adding a column/button."""
    menu.addSeparator()
    action = menu.addAction("Learn && Review…")
    qconnect(action.triggered, lambda _checked=False, did=deck_id: show_deck_settings(did))


def register_hooks() -> None:
    gui_hooks.deck_browser_will_show_options_menu.append(on_deck_options_menu)
