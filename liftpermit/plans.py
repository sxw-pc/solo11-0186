"""Measured load, hook geometry, path exclusion, and signed plan versions."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json


# Dynamic factor used by the tower-crane rating and by the conservative
# rigid-static upper bound.  Measured tension-meter readings already contain
# the real dynamic effect and must not be multiplied by this factor again.
DYNAMIC_FACTOR = 1.1
_SHARE_TOL = 1e-9


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


def _hook_xy(hook):
    """Accept a bare [x, y] pair or a ``{point, sling_rfid}`` hook object."""
    return hook["point"] if isinstance(hook, dict) else hook


def _center_supported(center, hooks):
    hooks = [_hook_xy(h) for h in hooks]
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


def _triangle_shares(center, hooks):
    """Exact barycentric static shares for a three-hook rigid body.

    Returns ``None`` when the geometry is not a non-degenerate triangle or the
    center is outside the hook triangle.  The weights are ratios only; they do
    not depend on the measured mass.
    """
    if len(hooks) != 3 or len(center) != 2 or len({tuple(p) for p in hooks}) != 3:
        return None
    try:
        (x1, y1), (x2, y2), (x3, y3) = (tuple(p) for p in hooks)
        x, y = center
        area2 = (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)
    except (TypeError, ValueError):
        return None
    if abs(area2) <= 1e-12:
        return None
    w1 = ((x2 - x) * (y3 - y) - (x3 - x) * (y2 - y)) / area2
    w2 = ((x3 - x) * (y1 - y) - (x1 - x) * (y3 - y)) / area2
    w3 = ((x1 - x) * (y2 - y) - (x2 - x) * (y1 - y)) / area2
    shares = [w1, w2, w3]
    if any(w < -_SHARE_TOL for w in shares) or abs(sum(shares) - 1.0) > 1e-9:
        return None
    return [max(0.0, w) for w in shares]


def _geometry_digest(hooks, center, bindings=None):
    payload = json.dumps({"hook_points": hooks, "center": center,
                          "hook_bindings": bindings or []},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _envelope(plan, shares):
    mass = float(plan.get("mass_kg", 0))
    walls = []
    worst = None
    hooks = [_hook_xy(h) for h in plan["event"].get("hook_points", [])]
    for index, (hook, share) in enumerate(zip(hooks, shares)):
        static_kg = mass * share
        envelope_kg = static_kg * DYNAMIC_FACTOR
        item = {"index": index, "hook_point": hook, "static_share": share,
                "static_kg": static_kg, "dynamic_factor": DYNAMIC_FACTOR,
                "envelope_kg": envelope_kg}
        walls.append(item)
        if worst is None or envelope_kg > worst["envelope_kg"]:
            worst = item
    return {"model": "rigid_static_barycentric", "source": "analysis",
            "mass_kg": mass, "dynamic_factor": DYNAMIC_FACTOR,
            "hook_count": len(shares), "walls": walls, "worst": worst}


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
        hook_xy = [_hook_xy(h) for h in hooks]
        shares = _triangle_shares(event.get("center", []), hook_xy)
        bindings = [((h or {}).get("sling_rfid") if isinstance(h, dict) else None) for h in hooks]
        geometry_sha = _geometry_digest(hooks, event.get("center", []), bindings)
        plan = {"event": event, "mass_kg": mass, "signed": signed,
                "geometry_sha": geometry_sha}
        # Only a real triangle has a rigorous static split.  For any other
        # hook count the envelope is unknown (``None``) rather than invented.
        envelope = _envelope(plan, shares) if shares else None
        result[job] = {
            "event": event, "version_conflict": conflict, "mass_kg": mass,
            "mass_event": measured["id"] if measured else None, "moment_kgm": mass * event["radius_m"],
            "center_ok": center_ok, "route_clear": clear, "signed": signed,
            "ready": ready, "status": "signed" if ready else "revision_conflict" if conflict else "pending_revision",
            "hook_points": hooks, "geometry_sha": geometry_sha,
            "load_share": {"model": "barycentric" if shares else "unsupported",
                           "shares": shares,
                           "supported": bool(center_ok and shares)},
            "envelope": envelope,
            "decisions": {
                "吊物重量版本核定": ("有效" if mass > 0 and not conflict else "待复测", "独立复测重量版本及力矩"),
                "吊点重心方案签认": ("签认" if center_ok and signed and not conflict else "撤回", "吊点包络覆盖重心并绑定方案版本"),
                "回转路径禁区核验": ("可通行" if clear else "重画", "几何路径与有效禁区时窗求交"),
                "方案签认撤回": ("有效" if signed and not conflict else "撤回", "旧方案签认不得沿用到新版本"),
            },
        }
    return result
