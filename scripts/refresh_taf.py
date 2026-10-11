"""Install the FAA TAF FY2025-FY2030 forward enplanement outlook (manual/
explicit use).

Usage: python3 scripts/refresh_taf.py

Downloads the official FAA TAF archive and parses Enplanements.xlsx with
stdlib `zipfile` + `xml.etree.ElementTree.iterparse` only (no openpyxl or
other runtime dependency). The parser itself lives in
`airport_intel/ingestion/live_sources.py`, re-exported here unchanged so existing tests
and this CLI keep working; the automatic SQLite startup refresh in
`airport_intel/ingestion/refresh.py` uses the same functions. This is a distinct
forward-looking indicator -- it is never summed into the historical
BTS-based opportunity score, and the refresh never overwrites the
last-good snapshot on failure.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from airport_intel.ingestion.live_sources import (  # noqa: E402
    TAF_FISCAL_PERIOD as FISCAL_PERIOD,
    TAF_GLOSSARY_URL as GLOSSARY_URL,
    TAF_SOURCE_PAGE as SOURCE_PAGE,
    TAF_SOURCE_URL as SOURCE_URL,
    build_faa_taf,
    download_taf_archive,
    extract_enplanements,
    taf_source_hash,
)

from refresh_data import OUTPUT, publish  # noqa: E402

MIN_REGISTRY_PAIRS = 500  # sanity floor; real data has ~795 (audit expectation, not hardcoded)


def _download(url: str) -> bytes:
    return download_taf_archive()


def main() -> None:
    snapshot = json.loads(OUTPUT.read_text(encoding="utf-8"))
    us_codes = {row["code"] for row in snapshot.get("airport_registry", []) if row.get("country") == "US"}
    if not us_codes:
        raise ValueError("No US-filtered airport registry installed; run scripts/refresh_registry.py first")

    raw = download_taf_archive()
    source_hash = taf_source_hash(raw)
    rows = extract_enplanements(raw)
    faa_taf, counts = build_faa_taf(rows, us_codes)

    if counts["registry_pairs"] < MIN_REGISTRY_PAIRS:
        raise ValueError(f"Too few US-registry locid pairs with both FY2025 and FY2030: {counts['registry_pairs']}")

    snapshot["faa_taf"] = faa_taf
    snapshot["sources"]["faa_taf"] = {
        "url": SOURCE_PAGE, "download": SOURCE_URL, "glossary": GLOSSARY_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": source_hash, "fiscal_period": FISCAL_PERIOD,
        "scope": "FAA TAF scenario=1 forecast total enplanements (aac+aat+commuter+us_flag+frgn_flag), FY2025 vs FY2030",
        "formula": "change_pct = (enplanements_2030 / enplanements_2025 - 1) * 100",
        "parsed_rows": len(rows), "registry_pairs": counts["registry_pairs"],
        "positive_pairs": counts["positive_pairs"], "published_airports": len(faa_taf),
    }
    publish(snapshot)
    print(f"Published faa_taf for {len(faa_taf)} US airports "
          f"(registry pairs: {counts['registry_pairs']}, positive pairs: {counts['positive_pairs']}, "
          f"source sha256: {source_hash})")


if __name__ == "__main__":
    main()
