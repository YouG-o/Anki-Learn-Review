from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from aqt import mw


ORIGINAL = "original"
ALTERNATIVE = "alternative"
CHOICES = (ORIGINAL, ALTERNATIVE)

_SETTINGS_DIRECTORY = "learn_review"
_SETTINGS_FILENAME = "settings.json"


def _settings_path() -> Path:
    """Keep preferences in the active Anki profile, outside the add-on folder."""
    return Path(mw.pm.profileFolder()) / _SETTINGS_DIRECTORY / _SETTINGS_FILENAME


def _read_settings(path: Path) -> dict[str, Any]:
    """Never silently replace an unreadable settings file with defaults."""
    with path.open("r", encoding="utf-8") as file:
        settings = json.load(file)
    if not isinstance(settings, dict) or not isinstance(settings.get("decks"), dict):
        raise ValueError(f"Invalid Learn & Review settings: {path}")
    return settings


def _legacy_settings() -> dict[str, Any]:
    """Migrate existing user data without changing or deleting its source."""
    # Earlier versions may have stored settings in the add-on's user_files.
    user_files_path = Path(__file__).resolve().parent / "user_files" / _SETTINGS_FILENAME
    if user_files_path.is_file():
        settings = _read_settings(user_files_path)
        if settings["decks"]:
            return settings

    # The original implementation stored preferences in Anki's add-on meta.json.
    # addonMeta reads that data even if the old config.json is no longer present.
    metadata = mw.addonManager.addonMeta(__package__)
    if isinstance(metadata, dict):
        settings = metadata.get("config")
        if isinstance(settings, dict) and isinstance(settings.get("decks"), dict):
            return settings

    return {"decks": {}}


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    """Atomically update the profile's settings without shipping personal data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".settings-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(settings, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _config() -> dict[str, Any]:
    """Load settings from the current profile; migrate only on first access."""
    path = _settings_path()
    if path.exists():
        return _read_settings(path)

    settings = _legacy_settings()
    _write_settings(path, settings)
    return settings


def get_deck_settings(deck_id: int) -> dict[str, Any]:
    """Return a private copy; missing decks always use native rendering."""
    deck = _config().get("decks", {}).get(str(deck_id), {})
    if not isinstance(deck, dict):
        deck = {}
    return {
        "learn": deck.get("learn", ORIGINAL),
        "review": deck.get("review", ORIGINAL),
        "alternatives": deepcopy(deck.get("alternatives", {})),
    }


def save_deck_settings(deck_id: int, settings: dict[str, Any]) -> None:
    """Persist only one deck, without overwriting settings of other decks."""
    for mode in ("learn", "review"):
        if settings.get(mode) not in CHOICES:
            raise ValueError(f"Invalid presentation choice for {mode}")

    alternatives: dict[str, dict[str, str]] = {}
    for model_id, templates in settings.get("alternatives", {}).items():
        if not isinstance(templates, dict):
            continue
        front = templates.get("front")
        back = templates.get("back")
        if isinstance(front, str) and isinstance(back, str) and front.strip() and back.strip():
            alternatives[str(int(model_id))] = {
                "front": front,
                "back": back,
                "css": templates.get("css", "") if isinstance(templates.get("css", ""), str) else "",
            }

    config = _config()
    decks = config.setdefault("decks", {})
    decks[str(deck_id)] = {
        "learn": settings["learn"],
        "review": settings["review"],
        "alternatives": alternatives,
    }
    _write_settings(_settings_path(), config)


def alternate_for(deck_id: int, mode: str, model_id: int) -> dict[str, str] | None:
    """Resolve one model's layout for an active session; otherwise no-op."""
    if mode not in ("learn", "review"):
        return None
    settings = get_deck_settings(deck_id)
    if settings[mode] != ALTERNATIVE:
        return None
    template = settings["alternatives"].get(str(model_id))
    if not isinstance(template, dict):
        return None
    if not all(isinstance(template.get(key), str) and template[key].strip() for key in ("front", "back")):
        return None
    return template
