"""Integrate independently runnable plan, resource, and site decisions."""
from __future__ import annotations

from . import plans, resources, tickets, work


def calculate(events):
    plan_events = [e for e in events if e["kind"] == "plan"]
    asset_events = [e for e in events if e["kind"] == "asset"]
    work_events = [e for e in events if e["kind"] == "work"]
    ticket_view = tickets.evaluate([e for e in events if e["kind"] == "ticket"])
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
    # 票域裁定作为方案账的证据层：车次只可聚合，构件归属必须待实体证据裁决。
    for trip_id, trip in sorted(ticket_view["trips"].items()):
        decisions[f"{trip_id}/车次磅单聚合"] = {
            "result": "已归属" if trip["status"] == "attributed" else "待核",
            "reason": "车次二维码指向车次而非构件，最新票联不得替代实体归属",
            "evidence": list(trip["ticket_ids"]),
        }
    for job_id, job in sorted(ticket_view["jobs"].items()):
        label = {"matched": "已归属", "conflict": "归属冲突", "pending": "待核"}[job["status"]]
        decisions[f"{job_id}/磅单构件归属"] = {
            "result": label,
            "reason": "封签台账绑定或独立监理现场复磅，缺实体证据时保持待核",
            "evidence": [claim["event_id"] for claim in job["claims"]],
        }
    for tid, agg in sorted(ticket_view["tickets"].items()):
        states.append(("吊装方案账", f"磅单:{agg['trip_id']}:{tid}", agg["status"], 1))
    for job_id, job in sorted(ticket_view["jobs"].items()):
        states.append(("吊装方案账", f"磅单归属:{job_id}", job["status"], 1))
    return decisions, states


def business_records(event):
    if event["kind"] == "ticket":
        subsystem = "吊装方案账"
        base = event["id"]
        records = []

        def add(entity_type, entity_id, body):
            records.append((subsystem, entity_type, entity_id, body))

        etype = event["type"]
        if etype == "capture":
            add("门岗磅单", f"{base}:capture", {
                "ticket_no": event.get("ticket_no"), "truck_qr": event["truck_qr"],
                "image_sha": event["image_sha"], "copy": event.get("copy"),
                "weighed_at": event["weighed_at"], "plate": event.get("plate"),
                "pallet_seals": event.get("pallet_seals", []),
                "unload_records": event.get("unload_records", []),
                "witness": event["witness"], "reading": event["reading_base"],
            })
        elif etype == "ocr_rerun":
            add("磅单OCR", f"{base}:ocr", {"image_sha": event["image_sha"],
                                          "candidate": event["candidate"], "at": event["at"]})
        elif etype == "manual_revision":
            add("磅单改判", f"{base}:revision", {
                "image_sha": event.get("image_sha"), "field": event["field"],
                "from": event["from"], "to": event["to"], "by": event["by"], "at": event["at"]})
        elif etype == "correction":
            add("磅单更正", f"{base}:correction", {
                "image_sha": event.get("image_sha"), "reading": event["reading"],
                "by": event["by"], "at": event["at"]})
        else:
            add("构件归属", f"{base}:attribution", {
                "component": event.get("component"), "basis": event["basis"],
                "seal": event.get("seal"), "net_kg": event.get("net_kg"),
                "witness": event["witness"], "at": event["at"],
                "status": event.get("status"), "supersedes": event.get("supersedes", []),
            })
        return records

    subsystem = {"plan": "吊装方案账", "asset": "设备与人员资格账",
                 "work": "现场许可与作业账"}[event["kind"]]
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
