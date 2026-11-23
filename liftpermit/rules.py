"""Integrate independently runnable plan, resource, and site decisions."""
from __future__ import annotations

from . import calendar_capture, plans, resources, work

CALENDAR_SUBSYSTEM = "日历采入与分配账"


def calculate(events):
    plan_events = [e for e in events if e["kind"] == "plan"]
    asset_events = [e for e in events if e["kind"] == "asset"]
    work_events = [e for e in events if e["kind"] == "work"]
    calendar_events = [e for e in events if e["kind"] == "calendar"]
    dispatch_events = [e for e in events if e["kind"] == "dispatch"]
    p = plans.evaluate(plan_events)
    view = (calendar_capture.evaluate(calendar_events, dispatch_events, p)
            if calendar_events or dispatch_events else None)
    allocation = view["allocation"] if view else None
    fleet = {c["id"]: c for c in view["fleet"]} if view else None
    a = resources.evaluate(asset_events, p, allocation=allocation, fleet=fleet)
    w = work.evaluate(work_events, p, a)
    decisions, states = {}, []
    for subsystem, accounts in [
        ("吊装方案账", p), ("设备与人员资格账", a),
        ("现场许可与作业账", w)
    ]:
        for job, account in accounts.items():
            event = account["event"]
            states.append((subsystem, job, account["status"], event["version"]))
            related = [event["id"]]
            for other in (p.get(job), a.get(job), w.get(job)):
                if other and other["event"]["id"] not in related:
                    related.append(other["event"]["id"])
            for name, (result, reason) in account["decisions"].items():
                decisions[f"{job}/{name}"] = {"result": result, "reason": reason,
                                               "evidence": related}
    if view and view["allocation"]:
        evidence = sorted({e["id"] for e in calendar_events} | {view["dispatch_id"]})
        version = view["dispatch_version"]
        for row in view["allocation"]["assignments"]:
            job = row["job_id"]
            note = f"{row['crane_id']}@{row['start']}"
            reason = ("当班完成协议:改派需触发新签认" if row["booking_resign"]
                      else "当班完成协议:原窗原机或授权改派" if view["protocol"] == "same_shift"
                      else "保留签认协议:签认不可自动转让")
            decisions[f"{job}/全局设备分配"] = {"result": note, "reason": reason,
                                                 "evidence": evidence}
            states.append((CALENDAR_SUBSYSTEM, job, f"allocated:{row['crane_id']}", version))
        for row in view["allocation"]["pending"]:
            job = row["job_id"]
            blocked = "; ".join(b["detail"] for b in row["blocked_by"]) or "无可行设备"
            decisions[f"{job}/全局设备分配"] = {"result": "待改期", "reason": blocked,
                                                 "evidence": evidence}
            states.append((CALENDAR_SUBSYSTEM, job, "reschedule", version))
    extras = {"calendar": view} if view else {}
    return decisions, states, extras


def business_records(event):
    subsystem = {"plan": "吊装方案账", "asset": "设备与人员资格账",
                 "work": "现场许可与作业账", "calendar": CALENDAR_SUBSYSTEM,
                 "dispatch": CALENDAR_SUBSYSTEM}[event["kind"]]
    base = event["id"]
    records = []

    def add(entity_type, entity_id, body):
        records.append((subsystem, entity_type, entity_id, body))

    if event["kind"] == "plan":
        for i, item in enumerate(event.get("measurements", [])):
            add("吊物测量", f"{base}:measure:{i}", item)
        add("吊点方案", f"{base}:scheme", {
            "job_id": event["job_id"], "center": event["center"],
            "hook_points": event["hook_points"], "radius_m": event["radius_m"],
            "boom_m": event["boom_m"], "route": event["route"],
            "signed_version": event["signed_version"],
        })
        for i, item in enumerate(event.get("zones", [])):
            add("禁入区公告", f"{base}:zone:{i}", item)
    elif event["kind"] == "asset":
        add("塔机检验", f"{base}:crane", event["crane"])
        for i, item in enumerate(event.get("slings", [])):
            add("索具证书", f"{base}:sling:{i}", item)
        add("人员排班", f"{base}:commander", event["commander"])
        add("塔机预留", f"{base}:own-reservation", {
            "job_id": event["job_id"], "crane_id": event["crane"]["id"],
            "start": event["window_start"], "end": event["window_end"],
            "status": event.get("reservation_status", "available"),
        })
        for i, item in enumerate(event.get("competing_reservations", [])):
            add("塔机预留", f"{base}:reservation:{i}", item)
    elif event["kind"] == "calendar":
        add("日历包", f"{base}:package", {
            "issuer": event["issuer"], "format": event["format"],
            "chars": len(event.get("text", "")),
        })
    elif event["kind"] == "dispatch":
        add("分配策略", f"{base}:policy", {
            "protocol": event.get("protocol"), "local_tz": event.get("local_tz"),
            "fleet": [c["id"] for c in event.get("fleet", [])],
        })
    else:
        add("作业许可", f"{base}:permit", {"job_id": event["job_id"],
                                      "actions": event.get("permit_actions", [])})
        for i, item in enumerate(event.get("landing_sites", [])):
            add("应急落点", f"{base}:site:{i}", item)
        for i, item in enumerate(event.get("wind_samples", [])):
            add("风速观测", f"{base}:wind:{i}", item)
        for i, item in enumerate(event.get("hook_events", [])):
            add("起落钩事件", f"{base}:hook:{i}", item)
    return records
