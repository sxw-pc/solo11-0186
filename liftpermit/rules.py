"""Integrate independently runnable plan, resource, and site decisions."""
from __future__ import annotations

from . import plans, resources, work


def calculate(events):
    plan_events = [e for e in events if e["kind"] == "plan"]
    asset_events = [e for e in events if e["kind"] == "asset"]
    work_events = [e for e in events if e["kind"] == "work"]
    p = plans.evaluate(plan_events)
    a = resources.evaluate(asset_events, p)
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
    return decisions, states


def business_records(event):
    subsystem = {"plan": "吊装方案账", "asset": "设备与人员资格账",
                 "work": "现场许可与作业账", "gis": "吊装方案账"}[event["kind"]]
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
    elif event["kind"] == "work":
        add("作业许可", f"{base}:permit", {"job_id": event["job_id"],
                                      "actions": event.get("permit_actions", [])})
        for i, item in enumerate(event.get("landing_sites", [])):
            add("应急落点", f"{base}:site:{i}", item)
        for i, item in enumerate(event.get("wind_samples", [])):
            add("风速观测", f"{base}:wind:{i}", item)
        for i, item in enumerate(event.get("hook_events", [])):
            add("起落钩事件", f"{base}:hook:{i}", item)
    else:
        add("公告版本", f"{base}:bulletin", {
            "bulletin_id": event["bulletin_id"], "revision": event["revision"],
            "effective_from": event["effective_from"],
            "effective_until": event["effective_until"],
            "revoked_at": event.get("revoked_at"),
            "retraction": event.get("retraction", "forward"),
            "error_m": event.get("error_m"),
            "error_source": event.get("error_source"),
        })
        for i, item in enumerate(event.get("zones", [])):
            add("禁入区公告", f"{base}:zone:{i}", item)
        if event.get("survey"):
            add("控制点复测", f"{base}:survey", event["survey"])
    return records
