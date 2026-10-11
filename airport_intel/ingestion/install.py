"""Install bundled one-time official exports into normalized SQLite tables.

The browser never reads these files directly. On the server's first boot,
each missing source is checksum-verified, bounded-parsed, validated, and
published through ``SourceRunPublisher``. Existing active runs are left
untouched, so warm startup performs only a small manifest/status check.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bundled_exports import (
    DELAY_SOURCE_URL,
    NPIAS_DOWNLOAD_URL,
    NPIAS_SOURCE_URL,
    T100_SOURCE_URL,
    file_sha256,
    parse_delay_causes_zip,
    parse_npias_xlsx,
    parse_t100_segments_zip,
)
from ..storage import FactReader, SourceRunPublisher

MANIFEST_FILENAME = "manifest.json"
EXPECTED_SOURCES = {
    "routes": ("bts_t100_segment", T100_SOURCE_URL),
    "delays": ("bts_delay_causes", DELAY_SOURCE_URL),
    "development": ("faa_npias", NPIAS_SOURCE_URL),
}


def _load_manifest(artifact_dir: Path) -> dict:
    path = artifact_dir / MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid bundled public-data manifest: {path}") from exc
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("artifacts"), dict):
        raise ValueError("Unsupported bundled public-data manifest schema")
    retrieved_at = manifest.get("retrieved_at")
    if not isinstance(retrieved_at, str) or not retrieved_at:
        raise ValueError("Bundled public-data manifest has no retrieval timestamp")
    return manifest


def _artifact(artifact_dir: Path, manifest: dict, key: str) -> tuple[Path, dict]:
    item = manifest["artifacts"].get(key)
    if not isinstance(item, dict):
        raise ValueError(f"Bundled public-data manifest is missing {key!r}")
    if item.get("source_url") != EXPECTED_SOURCES[key][1]:
        raise ValueError(f"Unexpected source URL for bundled artifact {key!r}")
    filename = item.get("filename")
    expected_hash = item.get("sha256")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ValueError(f"Unsafe bundled artifact filename for {key!r}")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError(f"Invalid checksum for bundled artifact {key!r}")
    path = artifact_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"Missing bundled public-data artifact: {path}")
    if file_sha256(path) != expected_hash:
        raise ValueError(f"Checksum mismatch for bundled artifact: {path}")
    return path, item


def install_bundled_public_data(
    publisher: SourceRunPublisher,
    reader: FactReader,
    artifact_dir: str | Path,
    *,
    only_missing: bool = True,
) -> dict:
    """Install verified bundled sources and return run ids plus coverage."""
    root = Path(artifact_dir)
    manifest = _load_manifest(root)
    active = set(reader.source_status()) if only_missing else set()
    results: dict[str, dict] = {}

    source, _ = EXPECTED_SOURCES["routes"]
    if source not in active:
        path, item = _artifact(root, manifest, "routes")
        rows, stats = parse_t100_segments_zip(path)
        run_id = publisher.publish_route_segments(rows, {
            "url": item["source_url"], "artifact": path.name,
            "artifact_sha256": item["sha256"], "retrieved_at": manifest["retrieved_at"],
            "period": item["period"],
            "scope": "US-origin T-100 Segment, US carriers, scheduled/non-scheduled passenger and cargo classes",
            "aggregation": "sum hidden carrier/aircraft dimensions by origin,destination,class,data_source,distance",
            "longhaul_denominator": "service class F departures with positive seats and departures",
            **stats,
        })
        results[source] = {"run_id": run_id, "coverage": stats}

    source, _ = EXPECTED_SOURCES["delays"]
    if source not in active:
        path, item = _artifact(root, manifest, "delays")
        rows, stats = parse_delay_causes_zip(path)
        run_id = publisher.publish_delay_months(rows, {
            "url": item["source_url"], "artifact": path.name,
            "artifact_sha256": item["sha256"], "retrieved_at": manifest["retrieved_at"],
            "period": item["period"],
            "scope": "BTS Airline Delay Cause, all reporting carriers and airports",
            "aggregation": "sum carrier rows by destination airport and month",
            "cause_share_denominator": "attributed carrier/weather/NAS/security/late-aircraft delay minutes",
            **stats,
        })
        results[source] = {"run_id": run_id, "coverage": stats}

    source, _ = EXPECTED_SOURCES["development"]
    if source not in active:
        path, item = _artifact(root, manifest, "development")
        if item.get("download_url") != NPIAS_DOWNLOAD_URL:
            raise ValueError("Unexpected NPIAS download URL in bundled manifest")
        rows, stats = parse_npias_xlsx(path)
        run_id = publisher.publish_development_plans(rows, {
            "url": item["source_url"], "download": item["download_url"],
            "artifact": path.name, "artifact_sha256": item["sha256"],
            "retrieved_at": manifest["retrieved_at"], "period": item["period"],
            "scope": "FAA NPIAS Appendix A all airports",
            "development_estimate_label": (
                "Five-year estimate across eligible airport development; not terminal-only CAPEX, "
                "committed funding, marginal cost, NPV, or profit"
            ),
            **stats,
        })
        results[source] = {"run_id": run_id, "coverage": stats}

    optional_sources = {value[0] for value in EXPECTED_SOURCES.values()}
    return {
        "installed": results,
        "skipped_active": sorted(active & optional_sources),
    }
