"""Crane rating, sling geometry, inspections, staff shifts, and reservations."""
from __future__ import annotations

import math
from collections import defaultdict


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    return {job: sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
            for job, items in grouped.items()}


def _covers(start, end, windows):
    return any(w["start"] <= start and w["end"] >= end for w in windows)


def evaluate(events, plans, handoffs=None):
    handoffs = handoffs or {}
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
        points = [p for p in crane.get("rating_curve", [])
                  if p["radius_m"] >= radius and p["boom_m"] >= boom]
        point = sorted(points, key=lambda p: (p["radius_m"], p["boom_m"], p["max_kg"]))[0] if points else None
        rated = point["max_kg"] if point else 0
        inspection_ok = (crane["inspected_until"] >= end and crane.get("status") == "good"
                         and not any(start <= x["at"] <= end for x in crane.get("faults", [])))
        crane_ok = bool(plan and mass > 0 and inspection_ok and rated >= mass * 1.1)
        slings = asset.get("slings", [])
        angle_loads = [s["capacity_kg"] * math.cos(math.radians(s["angle_deg"])) for s in slings]
        sling_count = len(plan["event"].get("hook_points", [])) if plan else 0
        sling_ok = (bool(plan and slings and len(slings) == sling_count)
                    and all(s["inspected_until"] >= end and s.get("status") == "good"
                            and 0 <= s["angle_deg"] < 80 for s in slings)
                    and all(load >= mass * 1.1 / sling_count for load in angle_loads))
        chain = handoffs.get(job)
        if chain is not None:
            coverage = chain["coverage"]
            staff_ok = coverage in ("proven", "conditional")
            staff_result = {"proven": "覆盖", "conditional": "附条件覆盖"}.get(coverage, "缺口")
        else:
            commander = asset["commander"]
            staff_ok = (commander["cert_until"] >= end
                        and _covers(start, end, commander.get("shifts", [])))
            staff_result = "覆盖" if staff_ok else "缺口"
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
            "handoff": chain,
            "decisions": {
                "塔机额定载荷复核": ("合格" if crane_ok else "超限或检验失效", "半径/臂长曲线、动态载荷与检验时窗"),
                "索具组合适配": ("合格" if sling_ok else "失配", "逐根索具的角度折减、吊点数量和检验期"),
                "指挥资质排班确认": (staff_result, "资质与连续班次或交接职责链覆盖全部作业窗口"),
                "设备保留冲突": ("冲突" if conflict else "无冲突", "同一塔机交叠的独立工单预留"),
            },
        }
    return result
