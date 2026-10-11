"""Add a compact global airport lookup index to the reproducible snapshot
(manual/explicit use).

OurAirports is public-domain, community-maintained geography, not an aviation
operations API. It never supplies investment KPIs. The fetch/parse logic
lives in `airport_intel/ingestion/live_sources.py`, shared with the automatic SQLite
startup refresh.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airport_intel.ingestion.live_sources import REGISTRY_INFO_PAGE, REGISTRY_SOURCE, fetch_global_registry  # noqa: E402

from refresh_data import OUTPUT, publish  # noqa: E402

SOURCE = REGISTRY_SOURCE


def fetch_registry() -> list[dict]:
    return fetch_global_registry()


def main() -> None:
    snapshot = json.loads(OUTPUT.read_text(encoding="utf-8"))
    registry = fetch_registry()
    snapshot["airport_registry"] = registry
    snapshot["sources"]["airport_registry"] = {
        "url": REGISTRY_INFO_PAGE,
        "download": SOURCE,
        "scope": "Global airport identity and geography only; no traffic, delay, or financial metrics",
        "license": "Public domain; community-maintained, not guaranteed complete or accurate",
    }
    publish(snapshot)
    print(f"Published {len(registry)} global airport lookup records")


if __name__ == "__main__":
    main()
