"""First-boot seeding from the bundled ``data/snapshot.json``.

Startup must serve answers before any network refresh finishes, so an empty
database is seeded from the reproducible bundled snapshot through the same
validated publisher a live refresh uses. Seeding is idempotent and crash-safe:
the database only counts as seeded once every required source is active.
"""

from __future__ import annotations

import json
from pathlib import Path

from .source_runs import SourceRunPublisher

BUNDLED_RETRIEVAL_BASIS = "snapshot-level retrieved_at; per-source retrieval time not recorded in the bundle"
_SOURCE_SECTIONS = {"version", "retrieved_at", "sources", "airport_registry", "airport_years", "faa_taf"}


def seed_from_snapshot(publisher: SourceRunPublisher, path: str | Path) -> bool:
    """Seed only an empty DB. Returns True when a seed was performed."""
    if publisher.is_seeded():
        repair_bootstrap_retrieval_times(publisher, path)
        return False
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    # Bundled manifests that carry no retrieval time inherit the snapshot's
    # own retrieval time, so first boot never stamps old data as "just fetched".
    bundled_at = snapshot.get("retrieved_at")
    source_manifests = {}
    for name, manifest in snapshot.get("sources", {}).items():
        source_manifests[name] = {"retrieved_at": bundled_at, "bootstrap": "bundled data/snapshot.json", **manifest}
        if not manifest.get("retrieved_at"):
            source_manifests[name]["retrieved_at_basis"] = BUNDLED_RETRIEVAL_BASIS
    extras = {key: value for key, value in snapshot.items() if key not in _SOURCE_SECTIONS}
    # Write the non-source extras *before* the source runs. `is_seeded()`
    # only turns true once all three source pointers exist, so a crash at
    # any point re-runs this idempotent seed instead of leaving an
    # activated database that is missing its extras.
    publisher.record_snapshot_metadata(str(snapshot.get("version", "unknown")), extras)
    publisher.publish_airports(snapshot.get("airport_registry", []), source_manifests.get("airport_registry", {}))
    publisher.publish_traffic(snapshot.get("airport_years", []), source_manifests.get("airport", {}))
    publisher.publish_taf(snapshot.get("faa_taf", {}), source_manifests.get("faa_taf", {}))
    return True


def repair_bootstrap_retrieval_times(publisher: SourceRunPublisher, path: str | Path) -> None:
    """Fix databases seeded before bundled runs inherited the snapshot time.

    Those runs were stamped with first-boot time, so they looked freshly
    fetched. A network refresh always records ``retrieved_at`` in its
    manifest, so a manifest without one (and without the newer
    ``bootstrap`` marker) can only be an old bundled seed. Idempotent.
    """
    with publisher.database.connect() as connection:
        stale = [
            (row["id"], json.loads(row["manifest_json"]))
            for row in connection.execute("SELECT id,manifest_json FROM source_runs")
        ]
        stale = [(run_id, manifest) for run_id, manifest in stale
                 if not manifest.get("retrieved_at") and "bootstrap" not in manifest]
        if not stale:
            return
        bundled_at = json.loads(Path(path).read_text(encoding="utf-8")).get("retrieved_at")
        if not bundled_at:
            return
        for run_id, manifest in stale:
            manifest.update({"bootstrap": "bundled data/snapshot.json",
                             "retrieved_at_basis": BUNDLED_RETRIEVAL_BASIS})
            connection.execute(
                "UPDATE source_runs SET retrieved_at=?, manifest_json=? WHERE id=?",
                (bundled_at, json.dumps(manifest, sort_keys=True), run_id),
            )
