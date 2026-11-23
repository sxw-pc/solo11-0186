"""Measured load, hook geometry, path exclusion, and signed plan versions."""
from __future__ import annotations

from collections import defaultdict
import json

from . import gis_capture


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


def _route_ruling(route, zones, start, end):
    """Rule every route segment against every active zone.

    Legacy ``rect`` zones keep their original axis-aligned rectangle
    semantics so migrated plans replay unchanged.  Normalized polygon
    ``rings`` zones are judged by gis_capture against the forbidden region,
    its boundary convention and its survey error band; a polygon is never
    silently replaced by its bounding rectangle.  References to bulletins
    that cannot be resolved fail closed.  Returns (clear, evidence) where
    evidence marks every checked segment 禁止/待测/可通行.
    """
    if len(route) < 2:
        return False, [{"verdict": gis_capture.VERDICT_FORBIDDEN,
                        "reason": "回转路径不足两点"}]
    clear = True
    evidence = []
    for zone in zones:
        if not isinstance(zone, dict):
            evidence.append({"verdict": gis_capture.VERDICT_FORBIDDEN,
                             "reason": "禁区记录无法解释"})
            clear = False
            continue
        if zone.get("void"):
            evidence.append({"verdict": gis_capture.VERDICT_CLEAR,
                             "zone_id": zone.get("zone_id"),
                             "reason": zone.get("void_reason", "公告已撤销")})
            continue
        if "rect" not in zone and "rings" not in zone:
            evidence.append({"verdict": gis_capture.VERDICT_FORBIDDEN,
                             "zone_id": zone.get("zone_id") or zone.get("bulletin"),
                             "reason": "禁区公告缺失或无法解释"})
            clear = False
            continue
        if not (zone.get("start", "") <= end and zone.get("end", "") >= start):
            continue
        if "rect" in zone:
            box = zone["rect"]
            if len(box) != 4 or box[0] > box[2] or box[1] > box[3]:
                evidence.append({"verdict": gis_capture.VERDICT_FORBIDDEN,
                                 "reason": "禁区矩形参数非法"})
                clear = False
                continue
            for i, (a, b) in enumerate(zip(route, route[1:])):
                if _segment_hits_box(a, b, box):
                    evidence.append({"verdict": gis_capture.VERDICT_FORBIDDEN,
                                     "reason": "线段穿越矩形禁区",
                                     "segment_index": i, "segment": [a, b]})
                    clear = False
        else:
            try:
                segment_evidence = gis_capture.route_zone_evidence(route, zone)
            except (ValueError, TypeError, KeyError, IndexError):
                evidence.append({"verdict": gis_capture.VERDICT_FORBIDDEN,
                                 "zone_id": zone.get("zone_id"),
                                 "reason": "禁区多边形几何非法"})
                clear = False
                continue
            for entry in segment_evidence:
                evidence.append(entry)
                if entry["verdict"] != gis_capture.VERDICT_CLEAR:
                    clear = False
    return clear, evidence


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
        clear, route_evidence = _route_ruling(event.get("route", []), event.get("zones", []),
                                              event["window_start"], event["window_end"])
        route_state = (gis_capture.VERDICT_FORBIDDEN
                       if any(e["verdict"] == gis_capture.VERDICT_FORBIDDEN for e in route_evidence)
                       else gis_capture.VERDICT_UNCERTAIN
                       if any(e["verdict"] == gis_capture.VERDICT_UNCERTAIN for e in route_evidence)
                       else gis_capture.VERDICT_CLEAR)
        signed = event.get("signed_version") == event["version"] and not event.get("revoked", False)
        ready = bool(mass > 0 and center_ok and clear and signed and not conflict)
        result[job] = {
            "event": event, "version_conflict": conflict, "mass_kg": mass,
            "mass_event": measured["id"] if measured else None, "moment_kgm": mass * event["radius_m"],
            "center_ok": center_ok, "route_clear": clear, "signed": signed,
            "route_state": route_state, "route_evidence": route_evidence,
            "ready": ready, "status": "signed" if ready else "revision_conflict" if conflict else "pending_revision",
            "decisions": {
                "吊物重量版本核定": ("有效" if mass > 0 and not conflict else "待复测", "独立复测重量版本及力矩"),
                "吊点重心方案签认": ("签认" if center_ok and signed and not conflict else "撤回", "吊点包络覆盖重心并绑定方案版本"),
                "回转路径禁区核验": ("可通行" if clear else "重画", "逐段路径与有效禁区时窗、边界约定及测量误差带求交"),
                "方案签认撤回": ("有效" if signed and not conflict else "撤回", "旧方案签认不得沿用到新版本"),
            },
        }
    return result
