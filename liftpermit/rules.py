"""Integrate independently runnable plan, resource, and site decisions."""
from __future__ import annotations

from . import plans, resources, tension_capture, work


def _choose_capture(events):
    """Latest capture batch per job, keeping ambiguous batches visible."""
    by_job = {}
    for event in events:
        by_job.setdefault(event["job_id"], []).append(event)
    chosen = {}
    for job, items in by_job.items():
        version = max(int(e.get("version", 1)) for e in items)
        candidates = [e for e in items if int(e.get("version", 1)) == version]
        selected = sorted(candidates, key=lambda e: (e.get("effective_at", ""), e["id"]))[-1]
        chosen[job] = selected
    return chosen


def _captures_for(events, plans, assets):
    """Materialise every capture against the asset version it was taken on.

    A capture names its plan geometry and the asset version whose RFID
    bindings it used.  Materialising against *that* asset keeps a historic
    overload measurable after a retrofit, instead of re-labelling the old
    reading through the new equalising-beam bindings.
    """
    from . import tension_capture
    by_job_asset = {}
    for event in events:
        if event["kind"] == "asset":
            by_job_asset.setdefault(event["job_id"], []).append(event)
    capture_events = [e for e in events if e["kind"] == "sling_capture"]
    chosen_plan = {}
    for job, batch in _choose_capture(capture_events).items():
        plan = plans.get(job)
        # Pick the asset event whose version the capture declares it used, so
        # versioned RFID bindings stay exact and a later retrofit cannot
        # re-judge an older reading.
        candidates = sorted(by_job_asset.get(job, []),
                            key=lambda e: (int(e.get("version", 1)),
                                           e.get("effective_at", ""), e["id"]))
        target_version = batch.get("asset_version")
        matching = [e for e in candidates if int(e.get("version", 1)) == target_version]
        selected = matching[-1] if matching else (candidates[-1] if candidates else None)
        if selected is not None:
            asset_account = resources.evaluate([selected], plans).get(job)
        else:
            asset_account = assets.get(job)
        chosen_plan[job] = tension_capture.loadshare(batch, plan, asset_account)
    return chosen_plan


def calculate(events):
    plan_events = [e for e in events if e["kind"] == "plan"]
    asset_events = [e for e in events if e["kind"] == "asset"]
    work_events = [e for e in events if e["kind"] == "work"]
    capture_events = [e for e in events if e["kind"] == "sling_capture"]
    p = plans.evaluate(plan_events)
    a = resources.evaluate(asset_events, p)
    captures = _captures_for(events, p, a)
    a = resources.evaluate(asset_events, p, captures)
    w = work.evaluate(work_events, p, a, captures)
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
                 "work": "现场许可与作业账",
                 "sling_capture": "索具实测账"}[event["kind"]]
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
        if isinstance(event.get("equalizing_beam"), dict):
            add("均载梁证书", f"{base}:beam", event["equalizing_beam"])
    elif event["kind"] == "sling_capture":
        # Every meter sample is persisted as its own reviewable row, together
        # with the plan geometry it claims to describe.
        add("拉力计批次", f"{base}:batch", {
            "job_id": event["job_id"], "capture_id": event.get("capture_id"),
            "plan_version": event.get("plan_version"),
            "geometry_sha": event.get("geometry_sha"),
            "captured_at": event.get("captured_at"),
            "signature": event.get("signature"), "digest": event.get("digest"),
            "alignment": event.get("alignment", {}),
        })
        for i, item in enumerate(event.get("events", [])):
            add("拉力计读数", f"{base}:reading:{i}", item)
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
