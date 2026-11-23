"""Crane rating, sling geometry, inspections, staff shifts, and reservations."""
from __future__ import annotations

import math
from collections import defaultdict

from . import extract, store


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    return {job: sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
            for job, items in grouped.items()}


def _covers(start, end, windows):
    return any(w["start"] <= start and w["end"] >= end for w in windows)


def _point(curve, radius, boom):
    points = [p for p in curve
              if p["radius_m"] >= radius and p["boom_m"] >= boom]
    point = sorted(points, key=lambda p: (p["radius_m"], p["boom_m"],
                                          p["max_kg"]))[0] if points else None
    return point["max_kg"] if point else 0


def _rating(event, db, radius, boom):
    """Return a dict describing the curve used for this asset.

    ``release_allowed`` is False whenever the event's curve is not backed
    by a certified, same-revision snapshot.  A historical archive curve is
    authoritative *for its own revision* (so V1 can release a job that V1
    covers), but the asset can only ever carry V1's real rows: the portal
    refuses to serve a V2 archive, so it can never stand in for current V2.
    """
    crane = event["crane"]
    verdict = (store.provenance_verdict(db, event) if db is not None
               else {"provenance": store.PROVENANCE_ATTESTED,
                     "blocked": False, "reason": "no_database"})
    prov = verdict.get("provenance")
    embedded = event.get("rating_curve", crane.get("rating_curve", []))
    info = {"provenance": prov, "reason": verdict.get("reason"),
            "revision": None, "curve_state": "embedded"}
    if verdict.get("blocked"):
        info["rated_kg"] = _point(embedded, radius, boom)
        info["curve_state"] = "unverified"
        info["release_allowed"] = False
        return info
    if prov == store.PROVENANCE_ATTESTED:
        if db is not None and verdict.get("snapshot_id"):
            published = extract.curve_for(db, crane["id"], radius, boom)
            info.update(rated_kg=published["rated_kg"],
                        revision=published["revision"],
                        curve_state="published")
        else:
            info["rated_kg"] = _point(embedded, radius, boom)
            info["curve_state"] = "embedded"
        info["release_allowed"] = True
        return info
    # A verified historical archive: authoritative for its own revision.
    published = (extract.curve_for(db, crane["id"], radius, boom)
                 if db is not None else
                 {"rated_kg": _point(embedded, radius, boom), "revision": None})
    info.update(rated_kg=published["rated_kg"], revision=published["revision"],
                curve_state="archived", release_allowed=True)
    return info


def evaluate(events, plans, db=None):
    latest = _choose(events)
    result = {}
    for job, asset in latest.items():
        plan = plans.get(job)
        start = plan["event"]["window_start"] if plan else asset["window_start"]
        end = plan["event"]["window_end"] if plan else asset["window_end"]
        mass = plan["mass_kg"] if plan else 0
        radius = plan["event"]["radius_m"] if plan else 0
        boom = plan["event"]["boom_m"] if plan else 0
        crane = asset["crane"]
        rating = _rating(asset, db, radius, boom)
        rated = rating["rated_kg"]
        inspection_ok = (crane["inspected_until"] >= end and crane.get("status") == "good"
                         and not any(start <= x["at"] <= end for x in crane.get("faults", [])))
        crane_ok = bool(plan and mass > 0 and inspection_ok
                        and rating["release_allowed"]
                        and rated >= mass * 1.1)
        slings = asset.get("slings", [])
        angle_loads = [s["capacity_kg"] * math.cos(math.radians(s["angle_deg"])) for s in slings]
        sling_count = len(plan["event"].get("hook_points", [])) if plan else 0
        sling_ok = (bool(plan and slings and len(slings) == sling_count)
                    and all(s["inspected_until"] >= end and s.get("status") == "good"
                            and 0 <= s["angle_deg"] < 80 for s in slings)
                    and all(load >= mass * 1.1 / sling_count for load in angle_loads))
        commander = asset["commander"]
        staff_ok = (commander["cert_until"] >= end
                    and _covers(start, end, commander.get("shifts", [])))
        overlap = [r for r in asset.get("competing_reservations", [])
                   if r["job_id"] != job and r["crane_id"] == crane["id"]
                   and r["start"] < end and r["end"] > start]
        conflict = bool(overlap)
        reservation = asset.get("reservation_status", "available")
        reserved = reservation == "reserved"
        ready = bool(plan and crane_ok and sling_ok and staff_ok and not conflict and reserved)
        status = ("fault" if crane.get("faults") or any(s.get("status") == "scrapped" for s in slings) else
                  "released" if reservation == "released" else
                  "reserved" if ready else "frozen")
        if not rating["release_allowed"] and status != "fault":
            status = "frozen"
        if rating["curve_state"] == "archived":
            rating_result = ("合格" if crane_ok
                             else "历史归档与当前作业不符，待核")
        elif not rating["release_allowed"]:
            rating_result = "曲线未审定，待核"
        else:
            rating_result = "合格" if crane_ok else "超限或检验失效"
        result[job] = {
            "event": asset, "ready": ready, "status": status,
            "rated_kg": rated, "crane_ok": crane_ok, "sling_ok": sling_ok,
            "staff_ok": staff_ok, "reservation_conflict": conflict,
            "rating_curve_state": rating["curve_state"],
            "rating_revision": rating["revision"],
            "rating_provenance": rating["provenance"],
            "rating_provenance_reason": rating["reason"],
            "release_allowed": rating["release_allowed"],
            "decisions": {
                "塔机额定载荷复核": (rating_result, "半径/臂长曲线、动态载荷与检验时窗"),
                "索具组合适配": ("合格" if sling_ok else "失配", "逐根索具的角度折减、吊点数量和检验期"),
                "指挥资质排班确认": ("覆盖" if staff_ok else "缺口", "资质与连续班次覆盖全部作业窗口"),
                "设备保留冲突": ("冲突" if conflict else "无冲突", "同一塔机交叠的独立工单预留"),
            },
        }
    return result


def build_asset_event(base, curve, *, snapshot_id=None, doc_id=None,
                      revision=None, status=None):
    """Build an asset event from an evidence-backed curve.

    Used by the end-to-end path so tests never hand-edit a rating curve.
    ``status`` defaults to ``observed`` for a snapshot-backed curve and
    to ``absent`` when no snapshot is supplied.
    """
    import copy
    event = copy.deepcopy(base)
    event["crane"]["rating_curve"] = [dict(row) for row in curve]
    provenance = {"status": status or ("observed" if snapshot_id else "absent")}
    if snapshot_id:
        provenance["snapshot_id"] = snapshot_id
    if doc_id:
        provenance["doc_id"] = doc_id
    if revision:
        provenance["revision"] = revision
    event["rating_provenance"] = provenance
    return event

