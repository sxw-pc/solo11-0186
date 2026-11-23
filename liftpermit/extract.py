"""Rating-curve extraction from captured pages.

Two entry points, deliberately separate:

``rating_rows``
    Legacy, *provisional* merge.  It concatenates whatever bytes exist for
    a ``doc_id`` ordered by page number and does not look at the revision.
    It is the reproduction path for the known mixed-revision defect and is
    never used to feed ``asset.rating_curve``.

``snapshot_rows``
    Authoritative extraction.  Reads *only* the page rows pinned to one
    snapshot (hence one revision) and refuses to produce a curve unless the
    snapshot is certified and revision-pure.
"""

from __future__ import annotations

import json


def rating_rows(db, doc_id):
    """Provisional merge by page number only.  Not revision-aware."""
    rows = []
    for page in db.execute(
            "SELECT raw FROM vendor_page WHERE doc_id=? ORDER BY page_no",
            (doc_id,)):
        rows.extend(json.loads(page["raw"])["rows"])
    return rows


class MixedRevisionError(ValueError):
    """Raised instead of returning a curve assembled across revisions."""

    def __init__(self, revisions, doc_id=None):
        super().__init__("mixed_revision_curve")
        self.revisions = sorted(revisions)
        self.doc_id = doc_id


def snapshot_page_rows(db, snapshot_id):
    """Return ``[{page_no, revision, raw}]`` for a snapshot, or raise on mix."""
    snapshot = db.execute(
        "SELECT portal_id, doc_id, revision FROM vendor_snapshot "
        "WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError("unknown_snapshot")
    pages = []
    revisions = set()
    for link in db.execute(
            "SELECT page_no, sha256 FROM vendor_snapshot_page "
            "WHERE snapshot_id=? ORDER BY page_no", (snapshot_id,)).fetchall():
        candidates = db.execute(
            "SELECT revision, raw FROM vendor_capture "
            "WHERE portal_id=? AND doc_id=? AND sha256=?",
            (snapshot["portal_id"], snapshot["doc_id"],
             link["sha256"])).fetchall()
        if not candidates:
            raise ValueError(f"snapshot_page_bytes_missing:{link['page_no']}")
        for candidate in candidates:
            revisions.add(candidate["revision"])
        chosen = sorted(candidates, key=lambda r: r["revision"])[0]
        pages.append({"page_no": link["page_no"],
                      "revision": chosen["revision"],
                      "sha256": link["sha256"],
                      "raw": bytes(chosen["raw"])})
    if len(revisions) > 1:
        raise MixedRevisionError(revisions, snapshot["doc_id"])
    if revisions and revisions != {snapshot["revision"]}:
        raise MixedRevisionError(revisions | {snapshot["revision"]},
                                 snapshot["doc_id"])
    return pages


def snapshot_rows(db, snapshot_id):
    """Rows for one certified snapshot; raises on mix or uncertified state."""
    snapshot = db.execute(
        "SELECT state FROM vendor_snapshot WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError("unknown_snapshot")
    if snapshot["state"] != "certified":
        raise ValueError(f"snapshot_not_certified:{snapshot['state']}")
    rows = []
    for page in snapshot_page_rows(db, snapshot_id):
        rows.extend(json.loads(page["raw"])["rows"])
    return rows


def curve_for(db, crane_id, radius_m, boom_m):
    """Select the rated capacity from the latest *published* curve.

    Publication only happens from a certified snapshot, so this function
    can never see a half table.  If no curve has been published the result
    is a zero rating, which the resource rules treat as not released.
    """
    row = db.execute(
        "SELECT rows, revision, snapshot_id FROM asset_rating_curve "
        "WHERE crane_id=? ORDER BY published_at DESC, rowid DESC LIMIT 1",
        (crane_id,)).fetchone()
    if row is None:
        return {"rated_kg": 0, "revision": None, "snapshot_id": None,
                "published": False}
    points = [p for p in json.loads(row["rows"])
              if p["radius_m"] >= radius_m and p["boom_m"] >= boom_m]
    point = sorted(points, key=lambda p: (p["radius_m"], p["boom_m"],
                                          p["max_kg"]))[0] if points else None
    return {"rated_kg": point["max_kg"] if point else 0,
            "revision": row["revision"], "snapshot_id": row["snapshot_id"],
            "published": True}
