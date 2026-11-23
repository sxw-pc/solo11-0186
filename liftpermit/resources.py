"""Crane rating, sling geometry, inspections, staff shifts, and reservations.

The sling verdict deliberately reports several *different* sources instead of
one blended number:

* ``equal_division`` – the original mass x 1.1 / hook-count textbook check,
  kept so old JSON without sensor evidence keeps its old meaning.
* ``measured``       – synchronous off-ground tension per sling, converted
  with each meter's own calibration and zero.
* ``geometry``       – the signed hook coordinates and the rigid-static
  barycentric envelope from :mod:`liftpermit.plans`.
* ``equalizing_beam``– a certified spreader plus two-end calibrated meters.

``sling_ok`` is true only when one of those sources actually proves every
sling under its own derated capacity.
"""
from __future__ import annotations

import math
from collections import defaultdict

DYNAMIC_FACTOR = 1.1


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    return {job: sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
            for job, items in grouped.items()}


def _covers(start, end, windows):
    return any(w["start"] <= start and w["end"] >= end for w in windows)


def _derated(sling):
    angle = sling.get("angle_deg")
    capacity = sling.get("capacity_kg")
    if not isinstance(angle, (int, float)) or not isinstance(capacity, (int, float)):
        return None
    return capacity * math.cos(math.radians(angle))


def _inspection_ok(slings, end):
    return all(s.get("inspected_until", "") >= end and s.get("status") == "good"
               and isinstance(s.get("angle_deg"), (int, float))
               and 0 <= s["angle_deg"] < 80 for s in slings)


def _sling_rfid(sling):
    return str(sling.get("sling_rfid") or sling.get("rfid") or "").strip().upper()


def _equal_share_pass(plan, slings, mass, end):
    """Original per-sling textbook check, unchanged in meaning."""
    sling_count = len(plan["event"].get("hook_points", [])) if plan else 0
    if not (plan and slings and sling_count and len(slings) == sling_count):
        return False
    if not _inspection_ok(slings, end):
        return False
    return all(_derated(s) is not None and _derated(s) >= mass * DYNAMIC_FACTOR / sling_count
               for s in slings)


def _hook_sling_map(plan, slings):
    """Pair envelope walls with slings by RFID when declared, else by order."""
    if not plan or not slings:
        return None
    hooks = plan.get("hook_points", [])
    if len(hooks) != len(slings):
        return None
    by_rfid = {_sling_rfid(s): s for s in slings if _sling_rfid(s)}
    mapping = []
    for index, hook in enumerate(hooks):
        rfid = str((hook or {}).get("sling_rfid") or "").strip().upper() if isinstance(hook, dict) else ""
        sling = by_rfid.get(rfid) if rfid else None
        mapping.append(sling or slings[index])
    return mapping


def _geometry_envelope_ok(plan, slings, mass, end):
    """Conservative rigid-static per-hook envelope must fit the derated leg."""
    envelope = plan.get("envelope") if plan else None
    if not envelope or not plan.get("load_share", {}).get("shares"):
        return None
    mapping = _hook_sling_map(plan, slings)
    if mapping is None or not _inspection_ok(slings, end):
        return False
    for wall, sling in zip(envelope["walls"], mapping):
        limit = _derated(sling)
        if limit is None or wall["envelope_kg"] > limit + 1e-9:
            return False
    return True


def _measured_evidence(plan, slings, end, capture, asset_version=None):
    """A signed capture only applies to the exact geometry it was taken on."""
    if not capture:
        return None
    bound_version = capture.get("asset_version")
    version_applies = bound_version is None or bound_version == asset_version
    same_geometry = bool(plan is not None
                         and capture.get("geometry_sha") == plan.get("geometry_sha")
                         and capture.get("asset_binding_ok", True)
                         and version_applies)
    applies = bool(same_geometry and capture.get("measured_ok"))
    accepted = bool(applies and _inspection_ok(slings, end))
    return {
        "capture_id": capture.get("capture_id"),
        "status": capture.get("status"),
        "flags": capture.get("flags", []),
        "rows": capture.get("rows", []),
        "measured_total_kgf": capture.get("measured_total_kgf"),
        "captured_plan_version": capture.get("plan_version"),
        "captured_geometry_sha": capture.get("geometry_sha"),
        "applies_to_current_plan": same_geometry,
        "asset_binding_ok": bool(capture.get("asset_binding_ok", True)),
        "asset_version": capture.get("asset_version"),
        "current_asset_version": asset_version,
        "capture_asset_version": bound_version,
        "version_applies": version_applies,
        "accepted": accepted,
    }


def _beam_evidence(asset, plan, mass, end):
    """Certified equalising beam plus two-end calibrated meters."""
    kit = asset.get("equalizing_beam")
    if not isinstance(kit, dict):
        return None
    certificate = kit.get("certificate") or {}
    members = kit.get("slings") or []
    meters = kit.get("meters") or []
    scope = certificate.get("scope") or []
    if isinstance(scope, str):
        scope = [scope]
    distribution = certificate.get("distribution")
    if not isinstance(distribution, list) or len(distribution) != len(members):
        distribution = [1.0 / len(members)] * len(members) if members else []
    max_leg = certificate.get("max_leg_kg")
    total_mass = mass if mass else 0
    notes = []
    valid = certificate.get("valid_until", "")
    if valid < end:
        notes.append("均载梁证书超出有效期")
    if abs(sum(distribution) - 1.0) > 1e-6 or any(d < 0 for d in distribution):
        notes.append("均载梁分配比例不是合法分布")
    legs = []
    for index, member in enumerate(members):
        derated = _derated(member)
        limits = [x for x in (derated, max_leg) if isinstance(x, (int, float))]
        limit = min(limits) if limits else None
        peak = total_mass * DYNAMIC_FACTOR * distribution[index] if distribution else None
        legs.append({"index": index, "sling_id": member.get("id"),
                     "distribution": distribution[index] if distribution else None,
                     "peak_kg": peak, "limit_kg": limit,
                     "ok": bool(limit is not None and peak is not None and peak <= limit + 1e-9)})
        if not (member.get("status") == "good" and member.get("inspected_until", "") >= end):
            notes.append(f"均载梁索具 {member.get('id')} 检验失效")
    meter_total = sum(m.get("reading_kgf", 0) for m in meters
                      if isinstance(m.get("reading_kgf"), (int, float)))
    meters_calibrated = bool(len(meters) >= 2 and all(
        m.get("calibrated_until", "") >= end for m in meters))
    meter_consistent = bool(meters_calibrated and total_mass
                            and abs(meter_total - total_mass) <= max(0.05 * total_mass, 25))
    if not meters_calibrated:
        notes.append("缺少双端经校准拉力计")
    elif not meter_consistent:
        notes.append(f"双端拉力计合计 {meter_total:.0f}kgf 与复测 {total_mass:.0f}kg 不符")
    covers_off_ground = "off_ground" in scope
    covers_static = "static" in scope
    if not covers_static:
        notes.append("均载梁证书未覆盖静载")
    if not covers_off_ground:
        notes.append("均载梁证书只覆盖静载，未覆盖离地摆动，须待核")
    ok = bool(legs and all(leg["ok"] for leg in legs) and meters_calibrated
              and meter_consistent and covers_static and covers_off_ground
              and valid >= end and abs(sum(distribution) - 1.0) <= 1e-6)
    return {
        "beam_id": kit.get("id"), "certificate_id": certificate.get("id"),
        "scope": list(scope), "covers_off_ground": covers_off_ground,
        "legs": legs, "meter_total_kgf": meter_total if meters else None,
        "meters_calibrated": meters_calibrated, "meter_consistent": meter_consistent,
        "ok": ok, "notes": notes,
        "status": ("ok" if ok else "pending_off_ground_certificate"
                   if covers_static and not covers_off_ground else "rejected"),
    }


def evaluate(events, plans, captures=None):
    latest = _choose(events)
    captures = captures or {}
    result = {}
    for job, asset in latest.items():
        plan = plans.get(job)
        start = plan["event"]["window_start"] if plan else asset["window_start"]
        end = plan["event"]["window_end"] if plan else asset["window_end"]
        mass = plan["mass_kg"] if plan else 0
        radius = plan["event"]["radius_m"] if plan else 0
        boom = plan["event"]["boom_m"] if plan else 0
        crane = asset["crane"]
        points = [p for p in crane.get("rating_curve", [])
                  if p["radius_m"] >= radius and p["boom_m"] >= boom]
        point = sorted(points, key=lambda p: (p["radius_m"], p["boom_m"], p["max_kg"]))[0] if points else None
        rated = point["max_kg"] if point else 0
        inspection_ok = (crane["inspected_until"] >= end and crane.get("status") == "good"
                         and not any(start <= x["at"] <= end for x in crane.get("faults", [])))
        crane_ok = bool(plan and mass > 0 and inspection_ok and rated >= mass * 1.1)
        slings = asset.get("slings", [])
        capture = captures.get(job)

        equal_ok = _equal_share_pass(plan, slings, mass, end)
        geometry_ok = _geometry_envelope_ok(plan, slings, mass, end)
        measured = _measured_evidence(plan, slings, end, capture,
                                      asset.get("version"))
        beam = _beam_evidence(asset, plan, mass, end)
        measured_ok = bool(measured and measured["accepted"])
        beam_ok = bool(beam and beam["ok"])

        # Measured evidence describes the real lift and outranks the static
        # upper bound; otherwise the conservative envelope governs; only for a
        # geometry with no rigorous model do we fall back to equal division.
        if measured_ok:
            sling_ok = True
        elif beam_ok:
            sling_ok = True
        elif geometry_ok is not None:
            sling_ok = bool(geometry_ok)
        else:
            sling_ok = equal_ok

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
        result[job] = {
            "event": asset, "ready": ready, "status": status,
            "rated_kg": rated, "crane_ok": crane_ok, "sling_ok": sling_ok,
            "staff_ok": staff_ok, "reservation_conflict": conflict,
            "sling_sources": {
                "equal_division": {
                    "source": "assumed_equal_division",
                    "per_sling_kg": (mass * DYNAMIC_FACTOR / len(plan["event"]["hook_points"])
                                     if plan and plan["event"].get("hook_points") else None),
                    "ok": equal_ok,
                },
                "measured": measured,
                "geometry": (plan.get("envelope") if plan else None),
                "geometry_ok": geometry_ok,
                "equalizing_beam": beam,
            },
            "decisions": {
                "塔机额定载荷复核": ("合格" if crane_ok else "超限或检验失效", "半径/臂长曲线、动态载荷与检验时窗"),
                "索具组合适配": ("合格" if sling_ok else "失配",
                              "逐根索具的角度折减、吊点数量和检验期；"
                              "实测/等份/几何三源分列，不得以总张力替代逐根"),
                "指挥资质排班确认": ("覆盖" if staff_ok else "缺口", "资质与连续班次覆盖全部作业窗口"),
                "设备保留冲突": ("冲突" if conflict else "无冲突", "同一塔机交叠的独立工单预留"),
            },
        }
    return result
