"""Diff two silver snapshots: what was added, changed or removed (ADR 0015).

**Pairing (Decisions 1 and 2).** "The two most recent builds" means the newest
silver version of each of the two most recent *distinct content snapshots*, read
by the `BuildStamp` on each commit and fetched through Delta time travel. Never
adjacent version numbers, never `ingested_at`: three pulls with one content hash
are one snapshot and diff to nothing, which is the live state today. A rebuild
on the same snapshot (re-enrichment, a taxonomy edit) is the same world, so only
its newest version is paired.

The walk over history is `tables.walk_table_history` -- the same walk the silver
rebuild gate uses -- so the two can never disagree about which version is
current. It stops at a Delta operation nobody has classified; pairing across one
is refused and reported (`Pairing.barrier`) rather than diffed silently.

**Classification (Decision 6).** One comparison per `submission_number`:

- ``added``   -- not in the previous snapshot's silver.
- ``removed`` -- on the list in the previous snapshot, not in the current one.
  Silver never drops a row (it keeps each device's newest bronze row), so this is
  read off the stamps: a row whose `source_snapshot_id` differs from its version's
  commit stamp was not in that pull (ADR 0015 amendment). A row gone from silver
  entirely also counts.
- ``changed`` -- any other tracked field differs, including coming back on the
  list.

`source_snapshot_id` itself is not compared: every row is restamped by every
pull, which is lineage, not movement. `on_list` is compared in its place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from registry.config.settings import Settings, get_settings
from registry.schemas import DeviceRecord
from registry.transform.bronze_to_silver import SILVER_TABLE, BuildStamp

logger = logging.getLogger(__name__)

# Every silver field except the row stamp, plus the derived on_list flag.
TRACKED_FIELDS = tuple(
    sorted([f for f in DeviceRecord.model_fields if f != "source_snapshot_id"] + ["on_list"])
)


@dataclass(frozen=True)
class SnapshotVersion:
    """A silver version and the content snapshot its commit is stamped with."""

    version: int
    source_snapshot_id: str


@dataclass(frozen=True)
class Pairing:
    """The pair to diff (previous, current), or None; and why not, if a stop."""

    pair: tuple[SnapshotVersion, SnapshotVersion] | None
    # The unclassified Delta commit that stopped the walk before two snapshots
    # were found. Set only when it is the reason there is no pair.
    barrier: dict[str, Any] | None = None


@dataclass(frozen=True)
class Movement:
    """One submission's movement between two snapshots."""

    submission_number: str
    movement: str  # "added" | "changed" | "removed"
    changed_fields: tuple[str, ...]
    prev: dict[str, Any] | None
    curr: dict[str, Any] | None


def _on_list(row: dict[str, Any] | None, snapshot: str) -> bool:
    return row is not None and row.get("source_snapshot_id") == snapshot


def _tracked(row: dict[str, Any], snapshot: str) -> dict[str, Any]:
    values = {f: row.get(f) for f in TRACKED_FIELDS if f != "on_list"}
    values["on_list"] = _on_list(row, snapshot)
    return values


def classify(
    prev_rows: list[dict[str, Any]],
    curr_rows: list[dict[str, Any]],
    prev_snapshot: str,
    curr_snapshot: str,
) -> list[Movement]:
    """Classify every submission that moved, sorted by submission number.

    A snapshot compared with itself returns ``[]`` -- the structural guarantee
    behind "empty on identical snapshots".
    """
    prev = {r["submission_number"]: r for r in prev_rows}
    curr = {r["submission_number"]: r for r in curr_rows}
    moves: list[Movement] = []

    for key in sorted(prev.keys() | curr.keys()):
        before, after = prev.get(key), curr.get(key)
        if before is None:
            moves.append(Movement(key, "added", (), None, after))
            continue
        if after is None:
            moves.append(Movement(key, "removed", (), before, None))
            continue

        old, new = _tracked(before, prev_snapshot), _tracked(after, curr_snapshot)
        # Only fields both versions actually carry: an old version read by time
        # travel predates any field added since, and comparing a missing column
        # would mark every row changed.
        shared = {"on_list"} | (before.keys() & after.keys())
        changed = tuple(f for f in TRACKED_FIELDS if f in shared and old[f] != new[f])
        if not changed:
            continue
        movement = "removed" if old["on_list"] and not new["on_list"] else "changed"
        moves.append(Movement(key, movement, changed, before, after))

    return moves


def pull_size(rows: list[dict[str, Any]], snapshot: str) -> int:
    """How many silver rows the pull for ``snapshot`` actually carried."""
    return sum(1 for r in rows if _on_list(r, snapshot))


def _distinct_snapshots(writes: list[dict[str, Any]]) -> list[SnapshotVersion]:
    versions: list[SnapshotVersion] = []
    seen: set[str] = set()
    for entry in writes:  # newest first
        stamp = BuildStamp.from_metadata(entry["userMetadata"])
        if stamp is None:
            # Pre-ADR-0015 versions, RESTOREs, and an unknown-operation stop: not
            # pairable by content.
            continue
        if stamp.source_snapshot_id in seen:
            continue
        seen.add(stamp.source_snapshot_id)
        versions.append(SnapshotVersion(entry["version"], stamp.source_snapshot_id))
    return versions


def snapshot_versions(spark, settings: Settings | None = None) -> list[SnapshotVersion]:
    """The newest stamped silver version of each distinct snapshot, newest first."""
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, SILVER_TABLE):
        return []
    return _distinct_snapshots(tables.walk_table_history(spark, settings, SILVER_TABLE).writes)


def pairing(spark, settings: Settings | None = None) -> Pairing:
    """The two most recent distinct snapshots to diff, or why there are none."""
    from registry import tables

    settings = settings or get_settings()
    if not tables.table_exists(spark, settings, SILVER_TABLE):
        return Pairing(pair=None)
    walk = tables.walk_table_history(spark, settings, SILVER_TABLE)
    versions = _distinct_snapshots(walk.writes)
    if len(versions) >= 2:
        return Pairing(pair=(versions[1], versions[0]))
    if walk.barrier is not None:
        logger.warning(
            "Not diffing silver: unrecognised Delta operation %r at version %s sits "
            "between the snapshots, so the pair cannot be trusted",
            walk.barrier["operation"],
            walk.barrier["version"],
        )
    return Pairing(pair=None, barrier=walk.barrier)


def latest_pair(
    spark, settings: Settings | None = None
) -> tuple[SnapshotVersion, SnapshotVersion] | None:
    """(previous, current), or None when there are fewer than two snapshots."""
    return pairing(spark, settings).pair


def read_version(
    spark, settings: Settings | None, version: SnapshotVersion
) -> list[dict[str, Any]]:
    """Silver as it stood at ``version``, through Delta time travel (Decision 2)."""
    from registry import tables

    settings = settings or get_settings()
    df = tables.read_table(spark, settings, SILVER_TABLE, version=version.version)
    return [r.asDict() for r in df.collect()]
