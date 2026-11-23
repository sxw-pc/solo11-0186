"""Measured load, hook geometry, path exclusion, and signed plan versions."""
from __future__ import annotations

from collections import defaultdict
import json


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
        verified = [x for x in measures if x.get("type") == "independent" and x.get("witness")
                    and x.get("at", "") <= event["window_end"]]
        measured = sorted(verified, key=lambda x: (x["at"], x["witness"]))[-1] if verified else None
        mass = measured["kg"] if measured else 0
        hooks = event.get("hook_points", [])
        center_ok = _center_supported(event.get("center", []), hooks)
        clear = _route_clear(event.get("route", []), event.get("zones", []),
                             event["window_start"], event["window_end"])
        signed = event.get("signed_version") == event["version"] and not event.get("revoked", False)
        ready = bool(mass > 0 and center_ok and clear and signed and not conflict)
        result[job] = {
            "event": event, "version_conflict": conflict, "mass_kg": mass,
            "mass_event": measured["id"] if measured else None, "moment_kgm": mass * event["radius_m"],
            "center_ok": center_ok, "route_clear": clear, "signed": signed,
            "signed_version": event.get("signed_version"), "revoked": bool(event.get("revoked", False)),
            "version_identity": {
                "job_id": job, "plan_event_id": event["id"], "plan_version": int(event["version"]),
                "signed_version": event.get("signed_version"),
                "revoked": bool(event.get("revoked", False)), "version_conflict": conflict,
            },
            "ready": ready, "status": "signed" if ready else "revision_conflict" if conflict else "pending_revision",
            "decisions": {
                "吊物重量版本核定": ("有效" if mass > 0 and not conflict else "待复测", "独立复测重量版本及力矩"),
                "吊点重心方案签认": ("签认" if center_ok and signed and not conflict else "撤回", "吊点包络覆盖重心并绑定方案版本"),
                "回转路径禁区核验": ("可通行" if clear else "重画", "几何路径与有效禁区时窗求交"),
                "方案签认撤回": ("有效" if signed and not conflict else "撤回", "旧方案签认不得沿用到新版本"),
            },
        }
    return result
