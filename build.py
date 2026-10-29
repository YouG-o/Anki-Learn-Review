from __future__ import annotations

import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"


REQUIRED_FILES = (
    "__init__.py",
    "addon.json",
    "deck_browser.py",
    "deck_settings.py",
    "card_presentations.py",
    "presentation_config.py",
    "logger.py",
    "reviewer.py",
    "scheduler.py",
    "session.py",
    "session_manager.py",
)


def load_metadata() -> tuple[str, str]:
    metadata_file = ROOT / "addon.json"

    with metadata_file.open("r", encoding="utf-8") as file:
        metadata = json.load(file)

    try:
        name = metadata["name"]
        version = metadata["version"]
    except KeyError as error:
        raise SystemExit(
            f"Missing field in addon.json: {error.args[0]}"
        ) from error

    return name, version


def build() -> None:
    name, version = load_metadata()

    # Validate inputs before deleting an existing release.
    for filename in REQUIRED_FILES:
        if not (ROOT / filename).is_file():
            raise SystemExit(f"Required addon file not found: {filename}")

    # Clean previous build.
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    DIST_DIR.mkdir()
    zip_path = DIST_DIR / f"{name}_{version}.zip"

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for filename in REQUIRED_FILES:
            archive.write(ROOT / filename, arcname=f"{name}/{filename}")

    print(f"Built: {zip_path}")


if __name__ == "__main__":
    build()
