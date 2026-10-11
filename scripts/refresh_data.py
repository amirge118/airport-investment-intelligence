"""Build a versioned, reproducible public-data snapshot (manual/explicit use).

Usage: python3 scripts/refresh_data.py
No credentials are needed. Network failures never replace the last good snapshot.

This still produces `data/snapshot.json`, kept as a recoverable import/backup
artifact. The fetch/parse logic itself lives in
`airport_intel/ingestion/live_sources.py`, shared with the automatic SQLite startup refresh
in `airport_intel/ingestion/refresh.py` -- both paths use the same proven adapter.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airport_intel.ingestion.live_sources import BTS_DATASET, fetch_bts_airport_year  # noqa: E402

OUTPUT = ROOT / "data" / "snapshot.json"


def airport_year(year: int) -> list[dict]:
    return fetch_bts_airport_year(year)


def publish(snapshot: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(snapshot, file, separators=(",", ":"), sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, OUTPUT)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> None:
    rows = airport_year(2023) + airport_year(2024) + airport_year(2025)
    for year in (2023, 2024, 2025):
        complete = [row for row in rows if row["year"] == year and row["months"] == 12
                    and row["passengers"] > 0 and row["seats"] >= row["passengers"]]
        if len(complete) < 500:
            raise ValueError(f"Insufficient complete BTS airports for {year}: {len(complete)}")
    snapshot = {
        "version": "2023-2025-annual-v2",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "airport": {"url": "https://data.bts.gov/d/r495-tyji", "api": BTS_DATASET, "period": "2023-01/2025-12", "scope": "T-100 airport-level all-carrier aggregates"}
        },
        "airport_years": rows,
    }
    # Preserve every prior section this script doesn't itself refresh,
    # including faa_taf -- the former defect that silently dropped it on a
    # BTS-only refresh is fixed by preserving every unknown top-level key
    # and its source manifest, not an explicit allowlist that must be kept
    # in sync by hand.
    if OUTPUT.exists():
        old = json.loads(OUTPUT.read_text(encoding="utf-8"))
        for key, value in old.items():
            if key not in ("version", "retrieved_at", "airport_years", "sources"):
                snapshot[key] = value
        for key, value in old.get("sources", {}).items():
            if key != "airport":
                snapshot["sources"][key] = value
    publish(snapshot)
    print(f"Published {OUTPUT} with {len(rows)} airport-year records")


if __name__ == "__main__":
    main()
