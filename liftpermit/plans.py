"""Measured load, hook geometry, path exclusion, and signed plan versions."""
from __future__ import annotations

from collections import defaultdict
import copy
import json

from . import tickets as ticket_domain


def _eligible_independent(measures, window_end):
    """当前有效的独立复测：撤回票留痕但不参选；票系测量须带已裁决归属。

    历史 fixture 的测量没有 provenance，视为既有来源继续有效，保证旧回归；
    门岗磅单写入的测量必须显式 provenance.status == "attributed"，
    车次 QR 聚合产生的待核测量即使没被撤回也不具备独立测量资格。
    """
    eligible = []
    for x in measures:
        if x.get("type") != "independent" or not x.get("witness"):
            continue
        if x.get("at", "") > window_end or x.get("revoked"):
            continue
        provenance = x.get("provenance")
        if provenance is not None and provenance.get("status") != "attributed":
            continue
        eligible.append(x)
    return eligible


def _segment_hits_box(a, b, box):
    """Closed segment/rectangle intersection, including a tangent at the boundary."""
    lo, hi = 0.0, 1.0
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    for origin, delta, lower, upper in (
        (x0, dx, box[0], box[2]), (y0, dy, box[1], box[3])
    ):
        if abs(delta) < 1e-12:
            if origin < lower or origin > upper:
                return False
            continue
        u, v = (lower - origin) / delta, (upper - origin) / delta
        lo, hi = max(lo, min(u, v)), min(hi, max(u, v))
        if lo > hi:
            return False
    return True


def _route_clear(route, zones, start, end):
    if len(route) < 2:
        return False
    for zone in zones:
        if zone["start"] <= end and zone["end"] >= start:
            box = zone["rect"]
            if len(box) != 4 or box[0] > box[2] or box[1] > box[3]:
                return False
            if any(_segment_hits_box(a, b, box) for a, b in zip(route, route[1:])):
                return False
    return True


def _center_supported(center, hooks):
    if len(hooks) < 2 or len(center) != 2:
        return False
    x, y = center
    if len(hooks) == 2:
        a, b = hooks
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = dx * dx + dy * dy
        if span == 0:
            return False
        t = ((x - a[0]) * dx + (y - a[1]) * dy) / span
        cross = (x - a[0]) * dy - (y - a[1]) * dx
        return 0 <= t <= 1 and abs(cross) <= 1e-6
    signs = []
    for a, b in zip(hooks, hooks[1:] + hooks[:1]):
        cross = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])
        if abs(cross) > 1e-6:
            signs.append(cross > 0)
    return bool(signs) and all(sign == signs[0] for sign in signs)


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    chosen = {}
    for job, records in grouped.items():
        version = max(int(e["version"]) for e in records)
        candidates = [e for e in records if int(e["version"]) == version]
        conflict = len({json.dumps({k: v for k, v in e.items() if k not in {'id', 'effective_at'}},
                                   sort_keys=True) for e in candidates}) > 1
        selected = sorted(candidates, key=lambda e: (e.get("effective_at", ""), e["id"]))[-1]
        chosen[job] = (selected, conflict)
    return chosen


def evaluate(events):
    result = {}
    for job, (event, conflict) in _choose(events).items():
        measures = event.get("measurements", [])
        verified = _eligible_independent(measures, event["window_end"])
        measured = sorted(verified, key=lambda x: (x["at"], x["witness"]))[-1] if verified else None
        mass = measured["kg"] if measured else 0
        # 票系测量的归属状态随票域重算，待核/冲突不能被“最新票优先”掩盖。
        attribution = (measured.get("provenance", {}) if measured and measured.get("provenance") else None)
        pending = any(m.get("type") == "independent" and not m.get("revoked")
                      and m.get("provenance", {}).get("status") == "pending"
                      for m in measures)
        hooks = event.get("hook_points", [])
        center_ok = _center_supported(event.get("center", []), hooks)
        clear = _route_clear(event.get("route", []), event.get("zones", []),
                             event["window_start"], event["window_end"])
        signed = event.get("signed_version") == event["version"] and not event.get("revoked", False)
        ready = bool(mass > 0 and center_ok and clear and signed and not conflict
                     and (attribution is None or attribution.get("status") == "attributed"))
        result[job] = {
            "event": event, "version_conflict": conflict, "mass_kg": mass,
            "mass_event": measured["id"] if measured else None, "moment_kgm": mass * event["radius_m"],
            "center_ok": center_ok, "route_clear": clear, "signed": signed,
            "attribution": attribution, "pending_ticket_attribution": pending,
            "ready": ready, "status": "signed" if ready else "revision_conflict" if conflict else "pending_revision",
            "decisions": {
                "吊物重量版本核定": (
                    "有效" if mass > 0 and not conflict
                    and (attribution is None or attribution.get("status") == "attributed")
                    else "待复测", "独立复测重量版本及力矩，票系测量须有构件实体归属"),
                "吊点重心方案签认": ("签认" if center_ok and signed and not conflict else "撤回", "吊点包络覆盖重心并绑定方案版本"),
                "回转路径禁区核验": ("可通行" if clear else "重画", "几何路径与有效禁区时窗求交"),
                "方案签认撤回": ("有效" if signed and not conflict else "撤回", "旧方案签认不得沿用到新版本"),
            },
        }
    return result


def _base_plan(events, job_id):
    """以该构件最新一版方案为模板，票域事件只重写测量，不擅动几何与时窗。"""
    chain = sorted((e for e in events if e.get("kind") == "plan" and e.get("job_id") == job_id),
                   key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))
    if not chain:
        raise ValueError(f"no existing plan for job {job_id}")
    return copy.deepcopy(chain[-1])


def build_ticket_plan_event(events, view, job_id, *, effective_at, event_id):
    """把已归属的磅单证据推广为一版新方案测量（仍需独立复签才放行）。

    新事件带 measurements 的完整历史：被票域新证据推翻的旧独立测量标记
    revoked=true 并保留 provenance 链；旧方案签认不再适用于新版本，
    signed_version 停留在旧版号，作业侧因此自然停工，不存在“旧 JSON 被覆盖
    而旧许可悄悄保持完工”。
    """
    job = ticket_domain.require_attributed(view, job_id)
    claim = job["claim"]
    base = _base_plan(events, job_id)
    next_version = max(int(e["version"]) for e in events
                       if e.get("kind") == "plan" and e.get("job_id") == job_id) + 1
    measures = base.get("measurements", [])
    prior_independent = [m for m in measures if m.get("type") == "independent"]
    retained = []
    for m in measures:
        item = copy.deepcopy(m)
        if m.get("type") == "independent":
            item["revoked"] = True
            item.setdefault("provenance", {})
            item["provenance"].setdefault("status", "superseded_by_ticket")
        retained.append(item)
    provenance = {
        "status": "attributed", "basis": claim["basis"],
        "claim_event": claim["event_id"], "witness": claim["witness"],
        "evidence": list(claim.get("evidence", [])),
        "images": list(claim.get("images", [])),
        "ticket_id": claim.get("ticket_id"),
    }
    retained.append({
        "id": f"{event_id}:measure:ticket",
        "type": "independent", "kg": job["net_kg"],
        "at": claim["at"], "witness": claim["witness"],
        "provenance": provenance,
    })
    event = {
        "id": event_id, "kind": "plan", "job_id": job_id,
        "version": next_version, "effective_at": effective_at,
        "window_start": base["window_start"], "window_end": base["window_end"],
        "radius_m": base["radius_m"], "boom_m": base["boom_m"],
        "measurements": retained,
        "center": copy.deepcopy(base["center"]),
        "hook_points": copy.deepcopy(base["hook_points"]),
        "route": copy.deepcopy(base["route"]),
        "zones": copy.deepcopy(base["zones"]),
        "signed_version": base.get("signed_version", base["version"]),
        "revoked": False,
        "promotion": {
            "from_ticket_claim": claim["event_id"], "net_kg": job["net_kg"],
            "superseded_measurements": [m["id"] for m in prior_independent],
            "requires_resign": True,
        },
    }
    return event


def build_countersign_event(events, job_id, *, effective_at, signer, event_id):
    """对最新方案版本独立复签；复签自身也是新版号，旧号签认不得沿用。

    推广产生的未签版本（signed_version 停留在旧号）与复签事件若共用版本号，
    会被 _choose 判为同版本冲突；因此复签递增一个版本号并签认该新号。
    """
    chain = [e for e in events if e.get("kind") == "plan" and e.get("job_id") == job_id]
    if not chain:
        raise ValueError(f"no existing plan for job {job_id}")
    latest = _base_plan(events, job_id)
    if latest.get("signed_version") == latest["version"] and not latest.get("revoked"):
        raise ValueError("latest plan version already signed")
    if not signer or not isinstance(signer, dict) or not signer.get("id") \
            or not signer.get("signature"):
        raise ValueError("countersign requires signed independent witness")
    next_version = max(int(e["version"]) for e in chain) + 1
    latest["id"] = event_id
    latest["version"] = next_version
    latest["effective_at"] = effective_at
    latest["signed_version"] = next_version
    latest["revoked"] = False
    latest["countersign"] = {"signer": signer, "signed_at": effective_at,
                             "countersigned_version": next_version}
    return latest
