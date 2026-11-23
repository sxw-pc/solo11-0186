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


def evaluate(events, plans):
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
        dynamic_kg = mass * 1.1
        inspection_ok = (crane["inspected_until"] >= end and crane.get("status") == "good"
                         and not any(start <= x["at"] <= end for x in crane.get("faults", [])))
        crane_ok = bool(plan and mass > 0 and inspection_ok and rated >= dynamic_kg)
        slings = asset.get("slings", [])
        angle_loads = [s["capacity_kg"] * math.cos(math.radians(s["angle_deg"])) for s in slings]
        sling_count = len(plan["event"].get("hook_points", [])) if plan else 0
        sling_ok = (bool(plan and slings and len(slings) == sling_count)
                    and all(s["inspected_until"] >= end and s.get("status") == "good"
                            and 0 <= s["angle_deg"] < 80 for s in slings)
                    and all(load >= dynamic_kg / sling_count for load in angle_loads))
        commander = asset["commander"]
        staff_ok = (commander["cert_until"] >= end
                    and _covers(start, end, commander.get("shifts", [])))
        overlap = [r for r in asset.get("competing_reservations", [])
                   if r["job_id"] != job and r["crane_id"] == crane["id"]
                   and r["start"] < end and r["end"] > start]
        conflict = bool(overlap)
        reservation = asset.get("reservation_status", "available")
        reserved = reservation == "reserved"
        ready = bool(plan and plan["ready"] and crane_ok and sling_ok
                     and staff_ok and not conflict and reserved)
        # 冻结须可解释：票归属待核直接视为方案未就绪，不允许拿“最新票”硬放行。
        freeze_reasons = []
        if not plan or not plan["ready"]:
            freeze_reasons.append("plan_not_ready" if plan else "missing_plan")
            if plan and plan.get("pending_ticket_attribution"):
                freeze_reasons.append("ticket_attribution_pending")
        if plan and not crane_ok:
            freeze_reasons.append("crane_rating_or_inspection")
        if plan and not sling_ok:
            freeze_reasons.append("sling_mismatch")
        if not staff_ok:
            freeze_reasons.append("staff_gap")
        if conflict:
            freeze_reasons.append("reservation_conflict")
        if not reserved:
            freeze_reasons.append("reservation_not_held")
        status = ("fault" if crane.get("faults") or any(s.get("status") == "scrapped" for s in slings) else
                  "released" if reservation == "released" else
                  "reserved" if ready else "frozen")
        result[job] = {
            "event": asset, "ready": ready, "status": status,
            "rated_kg": rated, "dynamic_demand_kg": dynamic_kg,
            "freeze_reasons": freeze_reasons,
            "crane_ok": crane_ok, "sling_ok": sling_ok,
            "staff_ok": staff_ok, "reservation_conflict": conflict,
            "decisions": {
                "塔机额定载荷复核": ("合格" if crane_ok else "超限或检验失效", "半径/臂长曲线、动态载荷与检验时窗"),
                "索具组合适配": ("合格" if sling_ok else "失配", "逐根索具的角度折减、吊点数量和检验期"),
                "指挥资质排班确认": ("覆盖" if staff_ok else "缺口", "资质与连续班次覆盖全部作业窗口"),
                "设备保留冲突": ("冲突" if conflict else "无冲突", "同一塔机交叠的独立工单预留"),
            },
        }
    return result
