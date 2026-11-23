"""Offline tension-meter capture batches and per-sling load-share accounting.

A capture batch is a signed, replayable record of what three clamp-on
dynamometers exported from a handheld terminal.  Every reading carries the
meter UID, the sling RFID it was bound to, the sample clock, the calibration
curve, the range, the zero offset, the raw Newton value and the lift-stage
label.  Readings are converted to kgf with that meter's own curve and zero,
then each sling's utilisation is computed only from readings that share one
``off_ground`` stage and are time aligned.

Nothing here averages a missing sling into the others: an incomplete triple is
reported as unpaired and leaves the utilisation unknown.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

# Conversion factor used by the meters' calibration certificate.
KG_PER_NEWTON = 1.0 / 9.80665

# A capture batch must identify the exact plan geometry it was taken against.
DEFAULT_ALIGN_TOL_S = 30

# Statuses that prove the lift stage itself was unsafe.  A geometry change
# alone (``stale``) does not: it only limits where the evidence applies.
CRITICAL_STATUSES = {"unverified", "overload", "over_range", "misbound"}

STATUS_PRIORITY = (
    "unverified",
    "overload",
    "over_range",
    "misbound",
    "unpaired",
    "uncalibrated",
    "stale",
    "ok",
)


def _parse(at):
    try:
        return datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return None


def _norm_rfid(value):
    return str(value or "").strip().upper()


def _meters(readings):
    meters = {}
    for reading in readings:
        meters.setdefault(reading.get("meter_uid"), []).append(reading)
    return meters


def _calibration(reading):
    return reading.get("calibration") or {}


def to_kgf(reading):
    """Convert one raw Newton sample with its own curve and zero offset.

    ``value_n`` is the raw instrument output.  The certificate supplies a
    gain (kgf per N) and a zero offset in Newton.  A missing or non-positive
    gain is not convertible and returns ``None`` instead of being treated as
    unity.
    """
    cal = _calibration(reading)
    gain = cal.get("kgf_per_n")
    if not isinstance(gain, (int, float)) or gain <= 0:
        return None
    zero_n = reading.get("zero_n", cal.get("zero_n", 0.0))
    raw = reading.get("value_n")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return (raw - zero_n) * gain


def _range_ok(reading):
    span = reading.get("range_n")
    raw = reading.get("value_n")
    if not isinstance(span, (int, float)) or span <= 0:
        return False
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return False
    return abs(raw) <= span


def _calibration_problem(reading, capture_at):
    cal = _calibration(reading)
    valid_until = _parse(cal.get("valid_until"))
    capture_dt = _parse(capture_at)
    if valid_until is None or capture_dt is None:
        return "校准曲线缺失"
    if capture_dt > valid_until:
        return "校准曲线已过期"
    issued = _parse(cal.get("issued_at"))
    if issued is not None and issued > capture_dt:
        return "校准签发晚于采样"
    return None


def _clock_problem(reading, reference_at, tol_s):
    at = _parse(reading.get("at"))
    if at is None:
        return "采样时刻无法解析"
    ref = _parse(reference_at)
    if ref is not None:
        drift = abs((at - ref).total_seconds())
        if drift > tol_s:
            return f"采样时钟漂移 {drift:.0f}s 超出 {tol_s}s"
    return None


def batch_digest(batch):
    """Stable digest the handheld terminal signs; also used to reseal in tests."""
    events = sorted(batch.get("events", []), key=lambda e: (e.get("at", ""), e.get("id", "")))
    payload = {"job_id": batch.get("job_id"),
               "capture_id": batch.get("capture_id"),
               "plan_version": batch.get("plan_version"),
               "asset_version": batch.get("asset_version"),
               "geometry_sha": batch.get("geometry_sha"),
               "events": events}
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def seal(batch, signature=None):
    batch = dict(batch)
    if signature is not None:
        batch["signature"] = signature
    batch["digest"] = batch_digest(batch)
    return batch


def loadshare(batch, plan, asset):
    """Pair three stages of readings and score measured per-sling utilisation.

    ``batch`` is one ``sling_capture`` event.  ``plan`` is the current
    ``plans.evaluate`` account (or ``None``).  ``asset`` is the current
    ``resources.evaluate`` account (or ``None``).

    The result always exposes three *different* sources of a share:

    * ``measured``   — synchronous off-ground tension / derated capacity
    * ``equal``      — the old mass x 1.1 / count textbook division
    * ``geometry``   — the rigid-static barycentric envelope from the plan

    A missing or contradictory piece of evidence is reported in ``flags`` and
    turns ``ok`` false; it is never silently replaced by mass / count.
    """
    events = sorted(batch.get("events", []), key=lambda e: (e.get("at", ""), e.get("id", "")))
    flags = []
    notes = []

    digest = batch_digest(batch)
    verified = bool(batch.get("signature")) and batch.get("digest") == digest
    if not verified:
        flags.append("unverified")

    readings = []
    for raw in events:
        item = dict(raw)
        item["kgf"] = to_kgf(raw)
        readings.append(item)

    capture_at = batch.get("captured_at") or (events[0].get("at") if events else None)
    plan_event = plan["event"] if plan else None
    reference_at = capture_at
    window = (plan_event.get("window_start"), plan_event.get("window_end")) if plan_event else (None, None)
    window_ok = bool(capture_at and window[0] and window[1]
                     and window[0] <= capture_at <= window[1]) if plan_event else True
    tol_s = int((batch.get("alignment") or {}).get("tolerance_s", DEFAULT_ALIGN_TOL_S))
    # Scope is decided by the *geometry* (hooks, center, bindings), not by the
    # administrative plan-version integer: a re-signed plan that keeps the same
    # hook points and center still describes the same lift.  A changed hook or
    # center yields a different digest and the old capture becomes stale.
    geometry_ok = bool(plan is not None
                       and batch.get("geometry_sha") == plan.get("geometry_sha")
                       and window_ok)
    if not geometry_ok:
        flags.append("stale")

    plan_hooks = plan.get("hook_points", []) if plan else []
    hook_ids = [h.get("id") if isinstance(h, dict) else i for i, h in enumerate(plan_hooks)]
    asset_event = (asset or {}).get("event") or {}
    asset_version = asset_event.get("version")
    asset_binding_ok = (batch.get("asset_version") is None
                        or batch.get("asset_version") == asset_version)
    if not asset_binding_ok:
        flags.append("misbound")
        notes.append(f"批次绑定资源版本 {batch.get('asset_version')}，"
                     f"当前资源为版本 {asset_version}")
    slings = asset_event.get("slings", [])
    sling_by_rfid = {}
    for sling in slings:
        rfid = _norm_rfid(sling.get("sling_rfid") or sling.get("rfid"))
        if rfid:
            sling_by_rfid[rfid] = sling

    # --- binding integrity: one RFID cannot be claimed by two meters ---------
    claims = {}
    for reading in readings:
        rfid = _norm_rfid(reading.get("sling_rfid"))
        if not rfid:
            flags.append("misbound")
            notes.append(f"仪表 {reading.get('meter_uid')} 未绑定索具 RFID")
            continue
        claims.setdefault(rfid, set()).add(reading.get("meter_uid"))
    for rfid, uids in claims.items():
        if len(uids) > 1:
            flags.append("misbound")
            notes.append(f"RFID {rfid} 被多只仪表认领: {sorted(uids)}")

    # --- meters must keep one identity / range / calibration all night ------
    for uid, samples in _meters(readings).items():
        rfids = {_norm_rfid(s.get("sling_rfid")) for s in samples}
        if len(rfids) > 1:
            flags.append("misbound")
            notes.append(f"仪表 {uid} 在批次内绑定多根 RFID: {sorted(rfids)}")
        values = {s.get("range_n") for s in samples}
        if len(values) > 1:
            flags.append("misbound")
            notes.append(f"仪表 {uid} 的量程在批次内被改动: {sorted(map(str, values))}")
        curves = {json.dumps(_calibration(s), sort_keys=True) for s in samples}
        if len(curves) > 1:
            flags.append("uncalibrated")
            notes.append(f"仪表 {uid} 的校准曲线在批次内被改动")
        if samples[0].get("zero_reset_at"):
            notes.append(f"仪表 {uid} 零点在 {samples[0]['zero_reset_at']} 重置；"
                         f"此前读数不得与之后读数配对")

    # --- per-reading conversion, range, calibration and clock checks --------
    for reading in readings:
        if reading["kgf"] is None:
            flags.append("uncalibrated")
            notes.append(f"仪表 {reading.get('meter_uid')} 缺少可用校准增益")
        problem = _calibration_problem(reading, capture_at)
        if problem:
            flags.append("uncalibrated")
            notes.append(f"仪表 {reading.get('meter_uid')}: {problem}")
        if not _range_ok(reading):
            flags.append("over_range")
            notes.append(f"仪表 {reading.get('meter_uid')} 原始值超出量程")
        if reading.get("stage") == "off_ground":
            clock = _clock_problem(reading, reference_at, tol_s)
            if clock:
                flags.append("uncalibrated")
                notes.append(f"仪表 {reading.get('meter_uid')}: {clock}")
        if reading["kgf"] is not None and reading["kgf"] < -1e-6:
            flags.append("uncalibrated")
            notes.append(f"仪表 {reading.get('meter_uid')} 换算为负值，零点不可信")
        if plan_hooks and reading.get("hook_id") not in hook_ids:
            flags.append("misbound")
            notes.append(f"仪表 {reading.get('meter_uid')} 的吊点 {reading.get('hook_id')} 不在当前方案内")

    # Only off-ground readings can prove lift-stage loading.
    off_ground = [r for r in readings if r.get("stage") == "off_ground"]
    pre_lift = [r for r in readings if r.get("stage") == "pre_lift"]
    post_lift = [r for r in readings if r.get("stage") == "post_lift"]
    excluded = {"pre_lift_peak": None, "post_lift_peak": None}
    for label, group in (("pre_lift_peak", pre_lift), ("post_lift_peak", post_lift)):
        values = [r["kgf"] for r in group if r["kgf"] is not None]
        if values:
            excluded[label] = max(values)
    if pre_lift or post_lift:
        notes.append("离地前试拉与离地后摆动峰值仅作排除记录，不参与离地阶段判定")
    if not off_ground:
        flags.append("unpaired")
        notes.append("批次没有离地阶段读数")

    # --- plan hook / sling binding -----------------------------------------
    # A plan may bind each hook to an RFID directly (authoritative).  Otherwise
    # the capture's own hook_id is used.  Either way the mapping must be 1:1.
    bound_hook = {}
    for index, hook in enumerate(plan_hooks):
        if not isinstance(hook, dict):
            continue
        rfid = _norm_rfid(hook.get("sling_rfid"))
        if rfid:
            bound_hook.setdefault(rfid, hook.get("id", index))
    rfid_hooks = dict(bound_hook)
    for reading in off_ground:
        rfid = _norm_rfid(reading.get("sling_rfid"))
        claimed = reading.get("hook_id")
        if rfid in bound_hook and bound_hook[rfid] != claimed:
            flags.append("misbound")
            notes.append(f"方案把 RFID {rfid} 绑到吊点 {bound_hook[rfid]}，仪表记为 {claimed}")
        rfid_hooks.setdefault(rfid, claimed)
    if len(plan_hooks) == 3 and len(set(rfid_hooks.values())) != 3:
        flags.append("misbound")
        notes.append("三吊点与索具不是一一绑定")

    # --- build per-sling evaluation (never backfill a missing sling) -------
    expected = list(range(len(plan_hooks))) if plan_hooks else sorted(
        {r.get("hook_index") for r in off_ground if r.get("hook_index") is not None})
    rows = {}
    for hook_index in expected:
        hook_id = hook_ids[hook_index] if plan_hooks else None
        matching = [r for r in off_ground
                    if (r.get("hook_id") == hook_id if plan_hooks
                        else r.get("hook_index") == hook_index)]
        row = {
            "hook_index": hook_index, "hook_id": hook_id,
            "sling_rfid": None, "sling_id": None,
            "capacity_kg": None, "angle_deg": None, "derated_kg": None,
            "measured_kgf": None, "measured_utilization": None,
            "measured_at": None, "meter_uid": None,
            "sample_count": len(matching), "status": "ok",
        }
        if len(matching) != 1:
            row["status"] = "unpaired"
            if not matching:
                notes.append(f"吊点 {hook_id} 缺少离地阶段同步读数")
            else:
                notes.append(f"吊点 {hook_id} 有 {len(matching)} 只仪表同时读数，无法配对")
            flags.append("unpaired")
        else:
            reading = matching[0]
            rfid = _norm_rfid(reading.get("sling_rfid"))
            sling = sling_by_rfid.get(rfid)
            row.update(sling_rfid=rfid, measured_kgf=reading["kgf"],
                       measured_at=reading.get("at"), meter_uid=reading.get("meter_uid"))
            # A bad sample on this sling is still about this sling: record it
            # on the row so it can never be averaged away by its neighbours.
            if reading["kgf"] is None or _calibration_problem(reading, capture_at) \
                    or _clock_problem(reading, reference_at, tol_s):
                row["status"] = "uncalibrated"
            elif not _range_ok(reading):
                row["status"] = "over_range"
            if sling is None:
                row["status"] = "misbound"
                flags.append("misbound")
                notes.append(f"吊点 {hook_id} 的 RFID {rfid} 不在当前资源账内")
            else:
                angle = sling.get("angle_deg")
                capacity = sling.get("capacity_kg")
                row.update(sling_id=sling.get("id"), capacity_kg=capacity, angle_deg=angle)
                if isinstance(capacity, (int, float)) and isinstance(angle, (int, float)):
                    derated = capacity * math.cos(math.radians(angle))
                    row["derated_kg"] = derated
                    if reading["kgf"] is not None and derated > 0:
                        row["measured_utilization"] = reading["kgf"] / derated
                        # Only a *trustworthy* sample can prove overload; a
                        # drifted clock or an out-of-range raw value stays the
                        # row's verdict even though the number looks high.
                        if (row["status"] == "ok"
                                and reading["kgf"] > derated + 1e-9):
                            row["status"] = "overload"
                            notes.append(
                                f"吊点 {hook_id} 索具 {sling.get('id')} 实测 "
                                f"{reading['kgf']:.0f}kgf 超折减能力 {derated:.0f}kgf")
        rows[hook_index] = row

    measured_total = sum(r["measured_kgf"] for r in rows.values()
                         if r["measured_kgf"] is not None)

    # Equal division from the old model, kept clearly labelled as *not measured*.
    mass = plan.get("mass_kg", 0) if plan else 0
    dynamic_total = mass * 1.1
    equal_share = dynamic_total / len(plan_hooks) if plan_hooks else None

    # Geometry envelope: only meaningful when the barycentric model applied.
    envelope = plan.get("envelope") if plan else None
    if envelope is None or not plan.get("load_share", {}).get("shares"):
        envelope = None

    # Cross-check: the measured total must be physically near the weighing.
    if plan and mass > 0 and off_ground and measured_total > 0:
        if abs(measured_total - mass) > max(0.15 * mass, 50):
            flags.append("unpaired")
            notes.append(f"离地阶段实测总张力 {measured_total:.0f}kgf 与独立复测 "
                         f"{mass:.0f}kg 偏离过大")

    # Roll the per-row verdicts up into the batch flags so a stale
    # reading-level flag cannot outrank the row that owns the sample.
    row_statuses = {r["status"] for r in rows.values()}
    for status_name in ("unpaired", "overload", "over_range", "uncalibrated", "misbound"):
        if status_name in row_statuses:
            flags.append(status_name)

    # A historic overload is a fact about the old lift even when the capture is
    # now judged against a different asset retrofit (e.g. an equalising beam
    # changed every RFID binding).  Keep that verdict, expose it separately.
    applied_overload = any(
        row["status"] == "overload" for row in rows.values())
    historical_overload = ("overload" in flags and
                           bool(plan is not None) and
                           batch.get("plan_version") == plan_event.get("version"))
    if applied_overload:
        historical_overload = True
    if historical_overload:
        flags.append("historical_overload")

    status = "ok"
    for candidate in STATUS_PRIORITY:
        if candidate in flags:
            status = candidate
            break

    return {
        "job_id": batch.get("job_id"), "capture_id": batch.get("capture_id"),
        "plan_version": batch.get("plan_version"),
        "current_plan_version": plan_event.get("version") if plan_event else None,
        "asset_version": batch.get("asset_version"),
        "current_asset_version": asset_version,
        "asset_binding_ok": asset_binding_ok,
        "geometry_sha": batch.get("geometry_sha"),
        "current_geometry_sha": plan.get("geometry_sha") if plan else None,
        "verified": verified, "digest": digest,
        "captured_at": capture_at, "stage": "off_ground",
        "rows": [rows[k] for k in sorted(rows)],
        "measured_total_kgf": measured_total if off_ground else None,
        "equal_share_kg": equal_share, "equal_share_source": "assumed_equal_division",
        "geometry": envelope,
        "excluded_peaks": excluded,
        "flags": sorted(set(flags)), "notes": notes,
        "historical_overload": historical_overload,
        "status": status, "measured_ok": status == "ok",
    }


def needs_emergency(retained):
    """Whether a retained capture proves the lift stage itself was unsafe.

    A stale-geometry or unpaired capture is a *scope* problem: it must not be
    applied to a new plan, but it also does not prove the old lift failed.
    """
    return bool(retained and retained.get("status") in CRITICAL_STATUSES)
