"""Calibrated wind, stop/resume permission, hook sequence, and landing duty."""
from __future__ import annotations

from collections import defaultdict
import math

from . import gis_capture


STAGES = ("hook", "lift", "land", "sign")


def _choose(events):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    return {job: sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
            for job, items in grouped.items()}


def _wind(samples, limit, start):
    valid = sorted([x for x in samples if x.get("calibrated_until", "") >= x.get("at", "")],
                   key=lambda x: (x["at"], x["id"]))
    high_at = None
    clear_at = None
    initial_clear = False
    for left, right in zip(valid, valid[1:]):
        independent = left["source"] != right["source"]
        if independent and right["at"] <= start and left["speed_mps"] <= limit and right["speed_mps"] <= limit:
            initial_clear = True
        if independent and left["speed_mps"] > limit and right["speed_mps"] > limit:
            high_at = right["at"]
            clear_at = None
        elif independent and high_at and left["at"] > high_at and right["at"] > high_at \
                and left["speed_mps"] <= limit and right["speed_mps"] <= limit:
            clear_at = right["at"]
    return high_at, clear_at, initial_clear


def _hook_state(items, commander):
    ordered = sorted(items, key=lambda x: (x["at"], x["id"]))
    stages = [x["stage"] for x in ordered]
    prefix = stages == list(STAGES[:len(stages)])
    independent_sign = bool(ordered and ordered[-1]["stage"] == "sign"
                            and ordered[-1].get("witness") not in {"", commander, None})
    completed = stages == list(STAGES) and independent_sign
    lifted = "lift" in stages and "land" not in stages
    return {"stages": stages, "ordered": prefix, "completed": completed,
            "lifted": lifted, "independent_sign": independent_sign}


def _site_zone_blocks(point, zones, at):
    """Reasons a landing point is blocked by the plan's zones at time ``at``.

    Uses the exact zone geometry resolved into the plan ledger (legacy rect
    or captured polygon with its error band), so the site ledger never rules
    against a different map version.  Anything uninterpretable blocks.
    """
    reasons = []
    for zone in zones:
        if not isinstance(zone, dict):
            reasons.append("禁区记录无法解释")
            continue
        if zone.get("void"):
            continue
        if "rect" not in zone and "rings" not in zone:
            reasons.append("禁区公告缺失或无法解释")
            continue
        if not (zone.get("start", "") <= at <= zone.get("end", "")):
            continue
        if "rect" in zone:
            box = zone["rect"]
            if len(box) != 4 or box[0] > box[2] or box[1] > box[3]:
                reasons.append("禁区矩形参数非法")
                continue
            if box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]:
                reasons.append("落点位于矩形禁区内")
        else:
            try:
                verdict, entry = gis_capture.point_zone_verdict(point, zone)
            except (ValueError, TypeError, KeyError, IndexError):
                reasons.append("禁区多边形几何非法")
                continue
            if verdict != gis_capture.VERDICT_CLEAR:
                reasons.append(f"落点{verdict}:{entry['reason']}")
    return reasons


def _safe_landing_site(event, plan):
    """Pick the nearest usable emergency landing site, with per-site evidence."""
    if not plan:
        return None, []
    mass = plan["mass_kg"]
    radius = plan["event"]["radius_m"]
    zones = plan["event"].get("zones", [])
    at = event.get("effective_at", "")
    candidates = []
    evidence = []
    for site in event.get("landing_sites", []):
        x, y = site["point"]
        distance = math.hypot(x, y)
        rejected = list(_site_zone_blocks(site["point"], zones, at))
        if site["capacity_kg"] < mass:
            rejected.append("落点承载不足")
        if site["available_until"] < at:
            rejected.append("落点已过期")
        if distance > radius:
            rejected.append("超出吊钩半径")
        usable = not rejected
        evidence.append({"site": site["id"], "usable": usable,
                         "reason": "可用" if usable else "；".join(rejected)})
        if usable:
            candidates.append((distance, site["id"]))
    chosen = sorted(candidates)[0][1] if candidates else None
    return chosen, evidence


def evaluate(events, plans, resources):
    result = {}
    for job, event in _choose(events).items():
        plan, asset = plans.get(job), resources.get(job)
        limit = event.get("wind_limit_mps", 10)
        start = plan["event"]["window_start"] if plan else event.get("effective_at", "")
        high_at, clear_at, calibrated = _wind(event.get("wind_samples", []), limit, start)
        commander = asset["event"]["commander"]["id"] if asset else ""
        hooks = _hook_state(event.get("hook_events", []), commander)
        actions = sorted(event.get("permit_actions", []), key=lambda x: (x["at"], x["id"]))
        release = [a for a in actions if a["action"] == "release"]
        resume = [a for a in actions if a["action"] == "resume"]
        permit_version = plan["event"]["version"] if plan else None
        released = bool(release and plan and release[-1]["plan_version"] == permit_version)
        wind_stopped = bool(high_at and not clear_at)
        resumed = bool(high_at and clear_at and resume and resume[-1]["at"] >= clear_at
                       and resume[-1]["plan_version"] == permit_version)
        wind_ok = calibrated and (not high_at or resumed)
        source_ready = bool(plan and plan["ready"] and asset and asset["ready"])
        active = source_ready and released and wind_ok and not hooks["lifted"]
        emergency = hooks["lifted"] and (wind_stopped or not source_ready)
        landing_site, landing_evidence = _safe_landing_site(event, plan) if emergency else (None, [])
        gate_at = resume[-1]["at"] if high_at and resumed else release[-1]["at"] if release else None
        hook_times = [h["at"] for h in event.get("hook_events", [])]
        hook_in_window = bool(plan and gate_at and hook_times
                              and min(hook_times) >= gate_at
                              and max(hook_times) <= plan["event"]["window_end"])
        completed = active and hooks["completed"] and hook_in_window
        status = ("finished" if completed else "emergency_landing" if emergency else
                  "suspended" if hooks["lifted"] else "paused" if wind_stopped else
                  "resumed" if resumed and active else "released" if active else "pending")
        result[job] = {
            "event": event, "status": status, "active": active, "completed": completed,
            "wind_stopped": wind_stopped, "wind_resumed": resumed,
            "hook_state": hooks, "emergency_landing": emergency,
            "landing_site": landing_site, "landing_evidence": landing_evidence,
            "decisions": {
                "现场风速停复工": ("放行" if active or completed else "停工", "双源连续校准风速、复工许可与当前方案版本"),
                "起落钩闭环核验": ("完工" if completed else "悬停或待核", "四阶段时序及独立签收"),
                "悬吊物应急落地": ((f"应急落地:{landing_site}" if landing_site else "无安全落点")
                                     if emergency else "无需", "离地未落钩遇停工或资源失格，按载荷、同一公告版本禁区与半径选择落点"),
                "监理独立见证": ("见证" if hooks["independent_sign"] else "待见证", "签收人与指挥员分离"),
            },
        }
    return result
