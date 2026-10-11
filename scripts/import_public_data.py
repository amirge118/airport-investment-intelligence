#!/usr/bin/env python3
"""Validate and publish the bundled one-time BTS/FAA public extracts.

This is intentionally an offline importer, not a startup refresh job. The
official source artifacts are already downloaded and source-manifested so a
reviewer can reproduce the database without depending on interactive forms.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from airport_intel.ingestion.install import install_bundled_public_data  # noqa: E402
from airport_intel.storage import FactReader, SQLiteDatabase, SourceRunPublisher  # noqa: E402


def import_public_data(database_path: Path, artifact_dir: Path) -> dict:
    database = SQLiteDatabase(database_path)
    publisher = SourceRunPublisher(database)
    result = install_bundled_public_data(
        publisher, FactReader(database), artifact_dir, only_missing=False,
    )
    return {"database": str(database_path), **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "airport_intel.sqlite3")
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "data" / "source_artifacts")
    args = parser.parse_args()
    result = import_public_data(args.database.resolve(), args.artifact_dir.resolve())
    import json
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
