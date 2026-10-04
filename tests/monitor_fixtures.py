"""Two synthetic silver snapshots that differ by a known set of rows.

Synthetic by necessity: bronze holds one distinct live snapshot, so there is no
real movement to slice (handoff §1). Every submission number below has one job,
named in its comment, and the tests assert on these numbers by value.

``snapA`` (previous) -> ``snapB`` (current):

========  ===========================================  ==========================
key       what happens                                 expected
========  ===========================================  ==========================
K100      radiology, identical in both                 nothing
K200      cardiovascular, identical in both            nothing (stock, not movement)
K300      panel Radiology -> Cardiovascular            changed, cardiometabolic lead
K400      name gains mortality language                changed, mortality_language
K500      cardiovascular, absent from snapB's pull     removed, cardiometabolic lead
K600      radiology, absent from snapB's pull          removed, not a lead
K700      new, cardiovascular                          added: new + cardiometabolic
K800      new, radiology, no other signal              added: new_submission only
K900      new, radiology, "sudden cardiac death"       added: new + mortality_language
DEN1000   new, life-sustaining per openFDA             added: new + life_sustaining
========  ===========================================  ==========================
"""

from __future__ import annotations

import datetime as dt

from registry.schemas import DeviceRecord

PREV = "snapA"
CURR = "snapB"

_PANELS = {"cardiovascular": "Cardiovascular", "radiology": "Radiology"}


def device(
    submission: str,
    snapshot: str,
    *,
    category: str = "radiology",
    name: str | None = None,
    panel: str | None = None,
    device_class: str | None = "II",
    decided: dt.date = dt.date(2025, 6, 1),
) -> dict:
    return DeviceRecord(
        submission_number=submission,
        device_name=name or f"Device {submission}",
        applicant_raw="Acme Medical, Inc.",
        applicant_resolved="Acme Medical",
        decision_date=decided,
        pathway="de_novo" if submission.startswith("DEN") else "510k",
        specialty_panel=panel or _PANELS[category],
        specialty_category=category,
        product_code="QIH",
        device_class=device_class,
        source_url=f"https://example.test/{submission}",
        source_snapshot_id=snapshot,
    ).model_dump()


NEW = dt.date(2026, 9, 1)  # clearance date of the devices new in snapB


def prev_rows() -> list[dict]:
    """Silver as built from snapA."""
    return [
        device("K100", PREV),
        device("K200", PREV, category="cardiovascular"),
        device("K300", PREV, category="radiology"),
        device("K400", PREV, name="Chest CT triage"),
        device("K500", PREV, category="cardiovascular"),
        device("K600", PREV),
    ]


def curr_rows() -> list[dict]:
    """Silver as built from snapB. Every row the snapB pull carried is restamped snapB; K500/K600 left the list,
    so silver keeps their last (snapA) rows."""
    keep = {r["submission_number"]: r for r in prev_rows()}
    restamp = lambda r: {**r, "source_snapshot_id": CURR}  # noqa: E731
    return [
        restamp(keep["K100"]),
        restamp(keep["K200"]),
        restamp(
            {
                **keep["K300"],
                "specialty_panel": "Cardiovascular",
                "specialty_category": "cardiovascular",
            }
        ),
        restamp({**keep["K400"], "device_name": "Chest CT triage with mortality risk score"}),
        keep["K500"],
        keep["K600"],
        device("K700", CURR, category="cardiovascular", decided=NEW),
        device("K800", CURR, decided=NEW),
        device("K900", CURR, name="Predictor of sudden cardiac death", decided=NEW),
        device("DEN1000", CURR, name="Ventilation assist", decided=NEW),
    ]
