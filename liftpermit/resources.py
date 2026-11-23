"""Crane rating, sling geometry, inspections, staff shifts, and reservations."""
from __future__ import annotations

import math
from collections import defaultdict

DYNAMIC_FACTOR = 1.1  # 动态载荷系数:额定能力须不低于独立复测重量的 1.1 倍


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    return {job: sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
            for job, items in grouped.items()}


def _covers(start, end, windows):
    return any(w["start"] <= start and w["end"] >= end for w in windows)


def rated_capacity(crane, radius_m, boom_m):
    """Rated capacity at a radius/boom: closest covering curve point, else 0."""
    points = [p for p in crane.get("rating_curve", [])
              if p["radius_m"] >= radius_m and p["boom_m"] >= boom_m]
    if not points:
        return 0
    point = sorted(points, key=lambda p: (p["radius_m"], p["boom_m"], p["max_kg"]))[0]
    return point["max_kg"]


def evaluate(events, plans, allocation=None, fleet=None):
    """Evaluate asset events against plans.

    When a calendar ``allocation`` table is supplied it is authoritative for the
    reservation facts of every job: an assigned job is rebound to the allocated
    crane/window (crane facts taken from ``fleet``), a pending job is marked
    ``pending`` so no stale reservation can be claimed.  Without an allocation,
    each event's own reservation fields are used (legacy behaviour).
    """
    latest = _choose(events)
    fleet = fleet or {}
    bindings = {row["job_id"]: row for row in (allocation or {}).get("assignments", [])}
    pending_jobs = {row["job_id"] for row in (allocation or {}).get("pending", [])}
    effective = {}
    for job, asset in latest.items():
        plan = plans.get(job)
        start = plan["event"]["window_start"] if plan else asset["window_start"]
        end = plan["event"]["window_end"] if plan else asset["window_end"]
        crane = asset["crane"]
        reservation = asset.get("reservation_status", "available")
        binding = bindings.get(job)
        if allocation is not None:
            if binding is not None:
                # 分配结果送入资源账:改派到全局可行的塔机与时段
                crane = fleet.get(binding["crane_id"], crane)
                start, end = binding["start"], binding["end"]
                reservation = "reserved"
            elif job in pending_jobs:
                # 待改期:不得伪称仍持有任何预留
                reservation = "pending"
        effective[job] = {"start": start, "end": end, "crane": crane, "reservation": reservation}
    result = {}
    for job, asset in latest.items():
        plan = plans.get(job)
        slot = effective[job]
        start, end = slot["start"], slot["end"]
        crane, reservation = slot["crane"], slot["reservation"]
        mass = plan["mass_kg"] if plan else 0
        radius = plan["event"]["radius_m"] if plan else 0
        boom = plan["event"]["boom_m"] if plan else 0
        rated = rated_capacity(crane, radius, boom)
        inspection_ok = (crane["inspected_until"] >= end and crane.get("status") == "good"
                         and not any(start <= x["at"] <= end for x in crane.get("faults", [])))
        crane_ok = bool(plan and mass > 0 and inspection_ok and rated >= mass * DYNAMIC_FACTOR)
        slings = asset.get("slings", [])
        angle_loads = [s["capacity_kg"] * math.cos(math.radians(s["angle_deg"])) for s in slings]
        sling_count = len(plan["event"].get("hook_points", [])) if plan else 0
        sling_ok = (bool(plan and slings and len(slings) == sling_count)
                    and all(s["inspected_until"] >= end and s.get("status") == "good"
                            and 0 <= s["angle_deg"] < 80 for s in slings)
                    and all(load >= mass * DYNAMIC_FACTOR / sling_count for load in angle_loads))
        commander = asset["commander"]
        staff_ok = (commander["cert_until"] >= end
                    and _covers(start, end, commander.get("shifts", [])))
        overlap = [r for r in asset.get("competing_reservations", [])
                   if r["job_id"] != job and r["crane_id"] == crane["id"]
                   and r["start"] < end and r["end"] > start]
        # 跨作业占用:其它工单在同一塔机同时段的有效预留同样构成冲突
        cross = [other for other, other_slot in effective.items()
                 if other != job and other_slot["reservation"] == "reserved"
                 and other_slot["crane"]["id"] == crane["id"]
                 and other_slot["start"] < end and other_slot["end"] > start]
        conflict = bool(overlap) or bool(cross)
        reserved = reservation == "reserved"
        ready = bool(plan and crane_ok and sling_ok and staff_ok and not conflict and reserved)
        status = ("fault" if crane.get("faults") or any(s.get("status") == "scrapped" for s in slings) else
                  "released" if reservation == "released" else
                  "reserved" if ready else
                  "reschedule" if reservation == "pending" else "frozen")
        result[job] = {
            "event": asset, "ready": ready, "status": status,
            "rated_kg": rated, "crane_ok": crane_ok, "sling_ok": sling_ok,
            "staff_ok": staff_ok, "reservation_conflict": conflict,
            "reservation_status": reservation, "assigned_crane": crane["id"],
            "decisions": {
                "塔机额定载荷复核": ("合格" if crane_ok else "超限或检验失效", "半径/臂长曲线、动态载荷与检验时窗"),
                "索具组合适配": ("合格" if sling_ok else "失配", "逐根索具的角度折减、吊点数量和检验期"),
                "指挥资质排班确认": ("覆盖" if staff_ok else "缺口", "资质与连续班次覆盖全部作业窗口"),
                "设备保留冲突": ("冲突" if conflict else "无冲突", "同一塔机交叠的独立工单预留"),
            },
        }
    return result
