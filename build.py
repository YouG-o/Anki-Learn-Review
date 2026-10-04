from __future__ import annotations

import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"


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
        )

    return name, version


def build() -> None:
    name, version = load_metadata()

    required_files = [
        "__init__.py",
    ]

    # Clean previous build.
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    DIST_DIR.mkdir()

    zip_path = DIST_DIR / f"{name}_{version}.zip"

    with ZipFile(
        zip_path,
        "w",
        compression=ZIP_DEFLATED,
    ) as archive:
        for filename in required_files:
            source = ROOT / filename

            if not source.is_file():
                raise SystemExit(
                    f"Required addon file not found: {filename}"
                )

            archive.write(
                source,
                arcname=f"{name}/{filename}",
            )

    print(f"Built: {zip_path}")


if __name__ == "__main__":
    build()