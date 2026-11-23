"""Calendar intake (ICS/CSV), stable instance identity, conflict graph, global allocation.

租赁公司的循环档期(ICS)与现场调度的逐日 CSV 在此归并:

- 展开 RRULE/EXDATE/RECURRENCE-ID;系列 UID 本身不是一次预约,只有展开出的
  具体实例才是。实例身份键为 ``发行方|系列|原实例起点(UTC)``,改期实例沿用原键,
  取消实例保留为指向旧实例的墓碑。
- 归并按 (stamp, 取消优先) 取最新事实:取消比原单先到、旧 CSV 晚到,都不会
  复活已被取消或改期的占用;同号 booking_id 跨发行方各自成键、互不干擾。
- 本地墙钟展开循环,UTC  instant 判重与查交叠;夏令时折返的歧义时刻确定性
  取 fold=0(第一次出现),因此重复导入与重算得到同一张实例表。
- 冲突图可按作业、时段、设备、能力与班次查询;全局分配器在容量/额定/签认
  约束下回溯求解,不按导入顺序逐作业贪心。

两种被授权的分配协议:

- ``same_shift``(当班完成):授权重签,签认过的预约可改派其它可行塔机,
  目标是在原窗口内完成最多工单;被改派的工单标记 ``booking_resign``,
  须触发新的签认后才算落定。
- ``preserve_signoff``(保留签认):签认不可自动转让,已签认工单钉在原机,
  无法安放的工单进入 ``pending``(待改期),不得伪称仍持有预留。

无解时 ``blocked_by`` 逐条列出是哪条容量、额定或签认约束挡住了哪个作业。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from zoneinfo import ZoneInfo

from . import plans
from .resources import DYNAMIC_FACTOR, rated_capacity

UTC = timezone.utc
MAX_INSTANCES = 400          # 单系列展开上限,保证离线可终止
SEARCH_BUDGET = 200_000      # 分配回溯节点上限
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
TRUTHY = {"yes", "true", "1", "y", "是", "取消"}


# ---------------------------------------------------------------- time helpers

def _tz(name):
    if not name or str(name).strip().upper() in {"UTC", "Z"}:
        return UTC
    return ZoneInfo(str(name).strip())


def _utc_iso(dt):
    return dt.astimezone(UTC).isoformat()


def _parse_iso(value, default_tz="UTC"):
    """Parse ISO-8601; naive values are read in default_tz. Returns an aware UTC dt."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(default_tz))
    return dt.astimezone(UTC)


def _truthy(value):
    return str(value or "").strip().lower() in TRUTHY


# ------------------------------------------------------------------ ICS parsing

def _unfold(text):
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _parse_ics_datetime(value, params, default_tz):
    value = value.strip()
    tzid = (params.get("TZID") or "").strip('"') or default_tz
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    fmt = "%Y%m%dT%H%M%S" if "T" in value else "%Y%m%d"
    naive = datetime.strptime(value, fmt)
    # 墙钟解释;夏令时折返的歧义/不存在时刻确定性取 fold=0
    return naive.replace(tzinfo=_tz(tzid))


_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_duration(text):
    match = _DURATION_RE.match(text or "")
    if not match:
        raise ValueError(f"unsupported DURATION: {text!r}")
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _parse_vevents(text):
    events, current = [], None
    for line in _unfold(text):
        head, sep, value = line.partition(":")
        if not sep:
            continue
        parts = head.split(";")
        name = parts[0].upper()
        params = {}
        for part in parts[1:]:
            key, _, val = part.partition("=")
            params[key.upper()] = val
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {"props": defaultdict(list), "params": {}}
        elif name == "END" and value.strip().upper() == "VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None:
            current["props"][name].append(value.strip())
            current["params"][name] = params
    return events


def _parse_rrule(text):
    rule = {}
    for part in text.split(";"):
        key, _, val = part.partition("=")
        rule[key.strip().upper()] = val.strip()
    return rule


def _vevent_record(raw, issuer, default_tz):
    props, params = raw["props"], raw["params"]

    def one(name, default=None):
        values = props.get(name)
        return values[-1] if values else default

    uid = one("UID")
    if not uid:
        raise ValueError("VEVENT missing UID")
    if not one("DTSTART"):
        raise ValueError(f"VEVENT {uid} missing DTSTART")
    start = _parse_ics_datetime(one("DTSTART"), params.get("DTSTART", {}), default_tz)
    if one("DTEND"):
        end = _parse_ics_datetime(one("DTEND"), params.get("DTEND", {}), default_tz)
    else:
        end = start + _parse_duration(one("DURATION", "PT0S"))
    stamp = one("DTSTAMP")
    stamp_dt = _parse_ics_datetime(stamp, params.get("DTSTAMP", {}), default_tz) if stamp else start
    recur = one("RECURRENCE-ID")
    tzid = (params.get("DTSTART", {}).get("TZID") or "").strip('"') or default_tz
    return {
        "uid": uid, "issuer": issuer, "tz": tzid,
        "start": start, "end": end, "stamp": stamp_dt,
        "sequence": int(one("SEQUENCE", "0") or 0),
        "rrule": _parse_rrule(one("RRULE")) if one("RRULE") else None,
        "exdates": [_parse_ics_datetime(item, params.get("EXDATE", {}), default_tz)
                    for value in props.get("EXDATE", []) for item in value.split(",") if item.strip()],
        "recurrence_id": _parse_ics_datetime(recur, params.get("RECURRENCE-ID", {}), default_tz) if recur else None,
        "status": (one("STATUS", "CONFIRMED") or "CONFIRMED").upper(),
        "job_id": one("X-LIFT-JOB-ID", "") or "",
        "crane": one("X-LIFT-CRANE", "") or "",
        "booking_id": one("X-LIFT-BOOKING-ID", "") or uid,
        "signed": _truthy(one("X-LIFT-SIGNED", "")),
        "shift": one("X-LIFT-SHIFT", "") or None,
    }


def _expand(start, rule):
    """Expand DTSTART+RRULE into aware datetimes; wall-clock preserved across DST."""
    if not rule:
        return [start]
    freq = rule.get("FREQ", "").upper()
    interval = int(rule.get("INTERVAL", "1") or 1)
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        raw = rule["UNTIL"]
        if raw.endswith("Z"):
            until = _parse_ics_datetime(raw, {}, "UTC")
        else:
            until = _parse_ics_datetime(raw, {}, str(start.tzinfo))
            if "T" not in raw:  # 日期形式的 UNTIL 含当日
                until += timedelta(days=1) - timedelta(microseconds=1)
    if count is None and until is None:
        raise ValueError("RRULE requires COUNT or UNTIL for bounded offline expansion")
    out = []

    def emit(candidate):
        if candidate < start:
            return
        if until is not None and candidate.astimezone(UTC) > until:
            return
        out.append(candidate)

    if freq == "DAILY":
        current = start
        while len(out) < MAX_INSTANCES:
            if until is not None and current.astimezone(UTC) > until:
                break
            emit(current)
            if count is not None and len(out) >= count:
                break
            current = current + timedelta(days=interval)
    elif freq == "WEEKLY":
        days = sorted({WEEKDAYS[d] for d in rule.get("BYDAY", "").split(",") if d in WEEKDAYS}
                      or {start.weekday()})
        monday = start - timedelta(days=start.weekday())
        week = 0
        while len(out) < MAX_INSTANCES:
            base = monday + timedelta(weeks=week * interval)
            if until is not None and base.astimezone(UTC) > until:
                break
            for day in days:
                emit(base + timedelta(days=day))
                if count is not None and len(out) >= count:
                    break
            if count is not None and len(out) >= count:
                break
            week += 1
    else:
        raise ValueError(f"unsupported RRULE FREQ: {freq!r}")
    out = sorted(set(out))[:MAX_INSTANCES]
    return out[:count] if count is not None else out


# ---------------------------------------------------------------- CSV parsing

_COLUMN_ALIASES = {
    "booking_id": ("booking_id", "booking", "id", "预约号"),
    "job_id": ("job_id", "job", "作业", "工单"),
    "crane": ("crane", "equipment", "crane_id", "设备", "塔机"),
    "tz": ("tz", "timezone", "时区"),
    "date": ("date", "日期"),
    "start": ("start", "start_time", "开始"),
    "end": ("end", "end_time", "结束"),
    "signed": ("signed", "签认"),
    "cancelled": ("cancelled", "cancel", "取消"),
    "updated_at": ("updated_at", "updated", "stamp", "更新时间"),
    "shift": ("shift", "班次"),
    "issuer": ("issuer", "发行方"),
}


def _canon_crane(value, aliases):
    text = (value or "").strip()
    return aliases.get(text.lower(), text)


def _row_datetime(value, date, tz):
    value = (value or "").strip()
    if "T" not in value:
        if not date:
            raise ValueError("CSV row needs a date column for time-only start/end")
        value = f"{date.strip()}T{value}"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(tz))
    return dt


def _csv_instances(text, issuer, default_tz, aliases):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return
    header = {name.strip().lower(): name for name in reader.fieldnames}

    def pick(row, field):
        for alias in _COLUMN_ALIASES[field]:
            if alias in header:
                return (row.get(header[alias]) or "").strip()
        return ""

    for row in reader:
        if not any((v or "").strip() for v in row.values()):
            continue
        tz = pick(row, "tz") or default_tz
        date = pick(row, "date")
        start = _row_datetime(pick(row, "start"), date, tz)
        end = _row_datetime(pick(row, "end"), date, tz)
        stamp_text = pick(row, "updated_at")
        stamp = _parse_iso(stamp_text, "UTC") if stamp_text else start
        yield _make_instance(
            pick(row, "issuer") or issuer, pick(row, "booking_id"), start, start, end, tz,
            job_id=pick(row, "job_id"),
            crane_id=_canon_crane(pick(row, "crane"), aliases),
            signed=_truthy(pick(row, "signed")),
            cancelled=_truthy(pick(row, "cancelled")),
            shift=pick(row, "shift") or None,
            source="csv", stamp=stamp, booking_id=pick(row, "booking_id"))


# ------------------------------------------------------------------- instances

def _make_instance(issuer, series, origin, start, end, tz, *, job_id, crane_id,
                   signed, cancelled, stamp, source, booking_id=None, shift=None, orphan=False):
    return {
        "key": f"{issuer}|{series}|{_utc_iso(origin)}",
        "issuer": issuer,
        "series": series,
        "booking_id": booking_id or series,
        "job_id": job_id,
        "crane_id": crane_id,
        "start_utc": _utc_iso(start),
        "end_utc": _utc_iso(end),
        "start_local": start.strftime("%Y-%m-%dT%H:%M"),
        "end_local": end.strftime("%Y-%m-%dT%H:%M"),
        "tz": tz,
        "signed": bool(signed),
        "cancelled": bool(cancelled),
        "orphan": bool(orphan),
        "shift": shift,
        "source": source,
        "stamp": _utc_iso(stamp),
    }


def _merge(table, inst):
    """Recency merge: newer stamp wins; on equal stamps a cancellation wins.

    因此取消可以先于原单到达,晚到的旧 CSV 也无法复活已被取消的占用。
    """
    old = table.get(inst["key"])
    if old is None or (inst["stamp"], inst["cancelled"]) >= (old["stamp"], old["cancelled"]):
        table[inst["key"]] = inst


def _merge_series(defs, overrides, rec):
    if rec["recurrence_id"] is None:
        key = (rec["issuer"], rec["uid"])
        old = defs.get(key)
        if old is None or (rec["stamp"], rec["sequence"]) >= (old["stamp"], old["sequence"]):
            defs[key] = rec
    else:
        key = (rec["issuer"], rec["uid"], _utc_iso(rec["recurrence_id"]))
        old = overrides.get(key)
        old_cancelled = old["status"] == "CANCELLED" if old else False
        if old is None or (rec["stamp"], rec["status"] == "CANCELLED") >= (old["stamp"], old_cancelled):
            overrides[key] = rec


def _ics_instance(definition, origin, start, end, *, cancelled, stamp, override, aliases, orphan=False):
    source = override or definition

    def field(name):
        return source.get(name) or definition.get(name)

    return _make_instance(
        definition["issuer"], definition["uid"], origin, start, end, definition["tz"],
        job_id=field("job_id") or "",
        crane_id=_canon_crane(field("crane"), aliases),
        signed=bool(field("signed")),
        cancelled=cancelled, orphan=orphan,
        shift=field("shift"), source="ics", stamp=stamp,
        booking_id=field("booking_id"))


def _series_instances(definition, override_map, aliases):
    """Expand one series definition and apply RECURRENCE-ID overrides/cancellations."""
    start = definition["start"]
    duration = definition["end"] - start
    excluded = {_utc_iso(x) for x in definition["exdates"]}
    out, seen = [], set()
    for occurrence in _expand(start, definition["rrule"]):
        instant = _utc_iso(occurrence)
        if instant in excluded:
            continue
        seen.add(instant)
        override = override_map.get(instant)
        if override is not None and override["status"] == "CANCELLED":
            # 取消墓碑:保留指向旧实例的原始时段
            out.append(_ics_instance(definition, occurrence, occurrence, occurrence + duration,
                                     cancelled=True, stamp=override["stamp"],
                                     override=override, aliases=aliases))
        elif override is not None:
            # 单次改期:身份键仍是原实例起点,窗口取新值
            out.append(_ics_instance(definition, occurrence, override["start"], override["end"],
                                     cancelled=False, stamp=override["stamp"],
                                     override=override, aliases=aliases))
        else:
            out.append(_ics_instance(definition, occurrence, occurrence, occurrence + duration,
                                     cancelled=False, stamp=definition["stamp"],
                                     override=None, aliases=aliases))
    for instant, override in override_map.items():
        if instant not in seen:
            # 孤儿覆盖(如系列已改单次):保留为指向旧实例的取消墓碑
            origin = override["recurrence_id"]
            out.append(_ics_instance(definition, origin, origin, origin + duration,
                                     cancelled=True, orphan=True, stamp=override["stamp"],
                                     override=override, aliases=aliases))
    return out


# ---------------------------------------------------------------- conflict graph

def build_graph(instances):
    """Nodes are reservation instances; edges join overlapping windows on one crane."""
    active = [i for i in instances if not i["cancelled"] and not i["orphan"]]
    edges = []
    for a, b in combinations(sorted(active, key=lambda i: i["key"]), 2):
        if a["crane_id"] and a["crane_id"] == b["crane_id"] \
                and a["start_utc"] < b["end_utc"] and b["start_utc"] < a["end_utc"]:
            edges.append({"a": a["key"], "b": b["key"], "crane_id": a["crane_id"],
                          "start": max(a["start_utc"], b["start_utc"]),
                          "end": min(a["end_utc"], b["end_utc"])})
    return {"instances": sorted(instances, key=lambda i: i["key"]), "edges": edges}


def annotate(graph, jobs, fleet):
    """Attach per-instance requirement/rating facts so the graph is capability-aware."""
    requirements = {j["job_id"]: j for j in jobs}
    cranes = {c["id"]: c for c in fleet}
    for inst in graph["instances"]:
        req = requirements.get(inst["job_id"])
        crane = cranes.get(inst["crane_id"])
        if req:
            inst["required_kg"] = round(req["mass_kg"] * DYNAMIC_FACTOR, 6)
        if req and crane:
            rated = rated_capacity(crane, req["radius_m"], req["boom_m"])
            inst["rated_kg"] = rated
            inst["capable"] = rated >= inst["required_kg"]
    return graph


def query(graph, *, job_id=None, crane_id=None, start=None, end=None, shift=None,
          capable=None, include_cancelled=False):
    """Query the conflict graph by job, window, equipment, capability, and shift."""
    out = []
    for inst in graph["instances"]:
        if not include_cancelled and (inst["cancelled"] or inst["orphan"]):
            continue
        if job_id is not None and inst["job_id"] != job_id:
            continue
        if crane_id is not None and inst["crane_id"] != crane_id:
            continue
        if shift is not None and inst.get("shift") != shift:
            continue
        if start is not None and inst["end_utc"] <= start:
            continue
        if end is not None and inst["start_utc"] >= end:
            continue
        if capable is not None and inst.get("capable") != capable:
            continue
        out.append(inst)
    return out


# ------------------------------------------------------------------ allocation

def _job_profiles(graph, jobs, tz):
    profiles = []
    active = [i for i in graph["instances"] if not i["cancelled"] and not i["orphan"]]
    for job in sorted(jobs, key=lambda j: j["job_id"]):
        start_utc = _utc_iso(_parse_iso(job["window_start"], tz))
        end_utc = _utc_iso(_parse_iso(job["window_end"], tz))
        matches = [i for i in active if i["job_id"] == job["job_id"]
                   and i["start_utc"] == start_utc and i["end_utc"] == end_utc]
        # 同一作业可能跨发行方重复预订:签认优先,其余按发行方/键字典序,结果确定
        matches.sort(key=lambda i: (not i["signed"], i["issuer"], i["key"]))
        current = matches[0] if matches else None
        profiles.append({
            **job,
            "required_kg": round(job["mass_kg"] * DYNAMIC_FACTOR, 6),
            "start_utc": start_utc,
            "end_utc": end_utc,
            "current_crane": current["crane_id"] if current else None,
            "current_key": current["key"] if current else None,
            "signed": bool(current and current["signed"]),
        })
    return profiles


def allocate(graph, jobs, fleet, protocol, tz="UTC"):
    """Globally solve job→crane placement under capacity/rating/sign-off constraints.

    protocol ``same_shift`` authorises re-signing (signed jobs may move cranes);
    ``preserve_signoff`` pins signed jobs to their signed crane.  The search
    maximises placements inside the original windows, then minimises moves,
    then takes the lexicographically smallest table — fully deterministic.
    """
    if protocol not in {"same_shift", "preserve_signoff"}:
        raise ValueError(f"unknown allocation protocol: {protocol!r}")
    cranes = sorted(fleet, key=lambda c: c["id"])
    profiles = _job_profiles(graph, jobs, tz)
    matched_keys = {p["current_key"] for p in profiles if p["current_key"]}
    external = [i for i in graph["instances"] if not i["cancelled"] and not i["orphan"]
                and i["key"] not in matched_keys]
    occupancy = {c["id"]: [(i["start_utc"], i["end_utc"], i["job_id"]) for i in external
                           if i["crane_id"] == c["id"]] for c in cranes}

    def capable(job, crane):
        return rated_capacity(crane, job["radius_m"], job["boom_m"]) >= job["required_kg"]

    def pinned(job, crane_id):
        return protocol == "preserve_signoff" and job["signed"] and crane_id != job["current_crane"]

    def free(job, crane_id):
        return all(not (job["start_utc"] < e and s < job["end_utc"])
                   for s, e, _ in occupancy[crane_id])

    best = {"score": None, "table": None, "assign": None}
    assignments = {}
    budget = [SEARCH_BUDGET]

    def search(index, placed, moved):
        budget[0] -= 1
        if budget[0] <= 0:
            raise RuntimeError("allocation search budget exceeded")
        if index == len(profiles):
            table = tuple(sorted(assignments.items()))
            score = (placed, -moved)
            if best["score"] is None or score > best["score"] \
                    or (score == best["score"] and table < best["table"]):
                best.update(score=score, table=table, assign=dict(assignments))
            return
        job = profiles[index]
        search(index + 1, placed, moved)  # 该作业本轮不安放(待改期)
        for crane in cranes:
            crane_id = crane["id"]
            if not capable(job, crane) or pinned(job, crane_id) or not free(job, crane_id):
                continue
            assignments[job["job_id"]] = crane_id
            occupancy[crane_id].append((job["start_utc"], job["end_utc"], job["job_id"]))
            search(index + 1, placed + 1, moved + (crane_id != job["current_crane"]))
            occupancy[crane_id].pop()
            del assignments[job["job_id"]]

    search(0, 0, 0)
    chosen = best["assign"] or {}
    rows, pending = [], []
    for job in profiles:
        job_id = job["job_id"]
        if job_id in chosen:
            crane_id = chosen[job_id]
            rows.append({
                "job_id": job_id, "crane_id": crane_id,
                "start": job["window_start"], "end": job["window_end"],
                "from_crane": job["current_crane"],
                "moved": crane_id != job["current_crane"],
                "booking_resign": bool(job["signed"] and crane_id != job["current_crane"]),
            })
        else:
            pending.append({"job_id": job_id, "status": "待改期",
                            "blocked_by": _blocked_reasons(job, cranes, chosen, profiles,
                                                           external, capable, protocol)})
    return {"protocol": protocol,
            "assignments": sorted(rows, key=lambda r: r["job_id"]),
            "pending": sorted(pending, key=lambda r: r["job_id"]),
            "objective": {"placed": len(rows), "moved": sum(1 for r in rows if r["moved"])}}


def _blocked_reasons(job, cranes, chosen, profiles, external, capable, protocol):
    """Explain, per crane, which capacity/rating/sign-off constraint blocks this job."""
    reasons = []
    holders = {job_id: crane_id for job_id, crane_id in chosen.items()}
    signed_holders = {p["job_id"]: p["signed"] for p in profiles}
    for crane in cranes:
        crane_id = crane["id"]
        rated = rated_capacity(crane, job["radius_m"], job["boom_m"])
        if rated < job["required_kg"]:
            reasons.append({"constraint": "rating", "crane_id": crane_id,
                            "rated_kg": rated, "required_kg": job["required_kg"],
                            "detail": f"{crane_id} 额定 {rated}kg 低于动态需求 {job['required_kg']}kg"})
            continue
        if protocol == "preserve_signoff" and job["signed"] and crane_id != job["current_crane"]:
            reasons.append({"constraint": "signoff", "crane_id": crane_id,
                            "detail": "保留签认协议下签认不可自动转让"})
            continue
        holder = next((j for j, c in holders.items() if c == crane_id and j != job["job_id"]), None)
        holder_inst = next((i for i in external if i["crane_id"] == crane_id
                            and i["start_utc"] < job["end_utc"] and i["end_utc"] > job["start_utc"]), None)
        if holder is not None:
            signed = signed_holders.get(holder, False)
            note = "；保留签认协议下不可自动转让" if signed and protocol == "preserve_signoff" else ""
            reasons.append({"constraint": "capacity", "crane_id": crane_id,
                            "holder": holder, "holder_signed": bool(signed),
                            "detail": f"{crane_id} 在 {job['window_start']} 至 {job['window_end']} "
                                      f"已被 {holder} 占用{note}"})
        elif holder_inst is not None:
            reasons.append({"constraint": "capacity", "crane_id": crane_id,
                            "holder": holder_inst["job_id"],
                            "holder_signed": bool(holder_inst["signed"]),
                            "detail": f"{crane_id} 在该时段已被 {holder_inst['job_id']} 的日历实例占用"})
    return reasons


def greedy_allocate(graph, jobs, fleet, tz="UTC"):
    """反例基线:按作业顺序取第一台局部空闲设备,不做全局重排也不校核额定。

    复现旧行为:J1 先占 TC1,J2 被派到额定不足的 TC2,留下错误的
    “无法同时完成”结论;真正的全局求解见 :func:`allocate`。
    """
    cranes = sorted(fleet, key=lambda c: c["id"])
    profiles = _job_profiles(graph, jobs, tz)
    matched_keys = {p["current_key"] for p in profiles if p["current_key"]}
    external = [i for i in graph["instances"] if not i["cancelled"] and not i["orphan"]
                and i["key"] not in matched_keys]
    occupancy = {c["id"]: [(i["start_utc"], i["end_utc"]) for i in external
                           if i["crane_id"] == c["id"]] for c in cranes}
    rows, pending = [], []
    for job in profiles:
        placed = None
        for crane in cranes:
            if all(not (job["start_utc"] < e and s < job["end_utc"]) for s, e in occupancy[crane["id"]]):
                placed = crane["id"]
                break
        if placed is None:
            pending.append({"job_id": job["job_id"], "status": "待改期",
                            "blocked_by": [{"constraint": "capacity",
                                            "detail": "局部空闲设备已取尽"}]})
            continue
        occupancy[placed].append((job["start_utc"], job["end_utc"]))
        rows.append({"job_id": job["job_id"], "crane_id": placed,
                     "start": job["window_start"], "end": job["window_end"],
                     "from_crane": job["current_crane"],
                     "moved": placed != job["current_crane"],
                     "booking_resign": False})
    return {"protocol": "greedy_first_fit",
            "assignments": sorted(rows, key=lambda r: r["job_id"]),
            "pending": sorted(pending, key=lambda r: r["job_id"]),
            "objective": {"placed": len(rows), "moved": sum(1 for r in rows if r["moved"])}}


# ------------------------------------------------------------- store integration

def calendar_event(package):
    """Turn an external calendar package (ICS/CSV text) into a deterministic event."""
    body = json.dumps({k: package[k] for k in ("issuer", "format", "text")},
                      ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return {"id": f"cal-{sha[:16]}", "kind": "calendar", "version": 1,
            "issuer": package["issuer"], "format": package["format"], "text": package["text"]}


def evaluate(calendar_events, dispatch_events, plan_accounts):
    """Merge all calendar packages into instances, build the graph, run allocation.

    每次重建都从全部 calendar/dispatch 事件确定性重算:外部日历读取成功而
    本地编排中断后,重放同一批事件会得到同一张实例表与分配表。
    """
    dispatch = None
    if dispatch_events:
        dispatch = sorted(dispatch_events,
                          key=lambda e: (int(e.get("version", 1)), e.get("effective_at", ""), e["id"]))[-1]
    default_tz = (dispatch or {}).get("local_tz") or "UTC"
    aliases = {str(k).strip().lower(): v
               for k, v in (dispatch or {}).get("equipment_aliases", {}).items()}
    fleet = list((dispatch or {}).get("fleet", []))
    table = {}
    defs, overrides = {}, {}
    for event in sorted(calendar_events, key=lambda e: e["id"]):
        fmt = event.get("format")
        if fmt == "ics":
            for raw in _parse_vevents(event["text"]):
                _merge_series(defs, overrides, _vevent_record(raw, event["issuer"], default_tz))
        elif fmt == "csv":
            for inst in _csv_instances(event["text"], event["issuer"], default_tz, aliases):
                _merge(table, inst)
        else:
            raise ValueError(f"unknown calendar format: {fmt!r}")
    override_by_uid = defaultdict(dict)
    for (issuer, uid, instant), rec in overrides.items():
        override_by_uid[(issuer, uid)][instant] = rec
    for key in sorted(set(defs) | set(override_by_uid)):
        issuer, uid = key
        definition = defs.get(key)
        override_map = override_by_uid.get(key, {})
        if definition is None:
            # 取消/改期比系列定义先到:先立孤儿墓碑,定义到达后归并
            for instant, rec in sorted(override_map.items()):
                origin = rec["recurrence_id"]
                _merge(table, _make_instance(
                    issuer, uid, origin, rec["start"], rec["end"], rec["tz"],
                    job_id=rec["job_id"], crane_id=_canon_crane(rec["crane"], aliases),
                    signed=rec["signed"], cancelled=True, orphan=True,
                    shift=rec["shift"], source="ics", stamp=rec["stamp"],
                    booking_id=rec["booking_id"]))
            continue
        for inst in _series_instances(definition, override_map, aliases):
            _merge(table, inst)
    graph = build_graph(sorted(table.values(), key=lambda i: i["key"]))
    jobs = [plans.requirement(job, account)
            for job, account in sorted(plan_accounts.items()) if account.get("ready")]
    annotate(graph, jobs, fleet)
    allocation = None
    if dispatch is not None and jobs and fleet:
        allocation = allocate(graph, jobs, fleet, dispatch.get("protocol", "preserve_signoff"),
                              tz=default_tz)
    return {"tz": default_tz,
            "protocol": (dispatch or {}).get("protocol"),
            "dispatch_id": (dispatch or {}).get("id"),
            "dispatch_version": int((dispatch or {}).get("version", 1)),
            "instances": graph["instances"], "edges": graph["edges"],
            "jobs": jobs, "fleet": fleet, "allocation": allocation}
