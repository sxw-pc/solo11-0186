"""离线值守采录与连续交接链。

读取两台离线刷卡机的签名刷卡、设备时钟误差、人员证照核验、岗位班次、
交接令牌、监理签章视频索引与作业方案版本，构建可核验的指挥员职责链：
每一段由哪位合格指挥员负责、交接是否有可证重叠、哪些时刻仍待核。
两台刷卡机各自离线、互不同步，纸面相同的显示刷卡时间不能直接推出无缝覆盖。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta

REQUIRED_CATEGORY = "tower_command"
BACKFILL_MINUTES = 15
EPOCH = datetime(1970, 1, 1)
FAR_FUTURE = datetime(2262, 1, 1)
_FMT = "%Y-%m-%dT%H:%M"


def _parse(text):
    return datetime.strptime(text, _FMT)


def _fmt(moment):
    return moment.strftime(_FMT)


def _shifted(text, minutes):
    return _parse(text) + timedelta(minutes=minutes)


def swipe_signature(reader, swipe):
    core = "|".join([reader["key"], swipe["person"], swipe["action"],
                     swipe["local_at"], str(swipe["seq"])])
    return hashlib.sha256(core.encode("utf-8")).hexdigest()


def token_signature(person, token):
    core = "|".join(["token", token["id"], token["from"], token["to"],
                     token["issued_at"], token["overlap_start"],
                     token["overlap_end"], person])
    return hashlib.sha256(core.encode("utf-8")).hexdigest()


def video_signature(video):
    segments = ";".join("{}-{}:{}".format(seg["start"], seg["end"],
                                          ",".join(sorted(seg["present"])))
                        for seg in video.get("segments", []))
    core = "|".join(["video", video["id"], video["monitor"], segments])
    return hashlib.sha256(core.encode("utf-8")).hexdigest()


def validate_events(events):
    """离线刷卡机采录入口的结构校验：读卡器、刷卡、人员与名册字段齐全。"""
    for event in events:
        for field in ("readers", "swipes", "people", "roster"):
            if not isinstance(event.get(field), list):
                raise ValueError(f"handoff capture missing {field}")
        for reader in event["readers"]:
            if not all(key in reader for key in ("id", "key", "clock_error_min")):
                raise ValueError("handoff reader missing id/key/clock_error_min")
        for swipe in event["swipes"]:
            if not all(key in swipe for key in
                       ("id", "reader", "person", "action", "local_at", "seq", "signature")):
                raise ValueError("handoff swipe missing required fields")


def _review_swipes(event):
    readers = {r["id"]: r for r in event.get("readers", [])}
    reviews, seen = [], set()
    for swipe in event.get("swipes", []):
        reader = readers.get(swipe.get("reader"))
        status = "verified"
        if reader is None:
            status = "unknown_reader"
        elif swipe.get("signature") != swipe_signature(reader, swipe):
            status = "bad_signature"
        if status == "verified":
            key = (swipe["reader"], swipe["seq"], swipe["signature"])
            if key in seen:
                status = "replay"
            seen.add(key)
        backfilled = False
        recorded = swipe.get("recorded_at")
        if status == "verified" and recorded:
            backfilled = _parse(recorded) > _shifted(swipe["local_at"], BACKFILL_MINUTES)
        reviews.append({"id": swipe["id"], "person": swipe["person"],
                        "action": swipe["action"], "local_at": swipe["local_at"],
                        "reader": swipe.get("reader"), "status": status,
                        "backfilled": backfilled})
    return reviews


def _proven_pins(event):
    """交接令牌须双方签名、签发时刻落在同场区间内，且监理签章视频完整覆盖该区间。"""
    videos = [v for v in event.get("witnesses", [])
              if v.get("signature") == video_signature(v)]
    pins, proof = defaultdict(list), []
    for token in event.get("tokens", []):
        parties = (token.get("from"), token.get("to"))
        signatures = token.get("signatures", {})
        if any(signatures.get(p) != token_signature(p, token) for p in parties):
            continue
        if not (token["overlap_start"] <= token["issued_at"] <= token["overlap_end"]):
            continue
        covered = any(seg["start"] <= token["overlap_start"]
                      and seg["end"] >= token["overlap_end"]
                      and all(p in seg.get("present", []) for p in parties)
                      for video in videos for seg in video.get("segments", []))
        if not covered:
            continue
        for party in parties:
            pins[party].append((token["overlap_start"], token["overlap_end"]))
        proof.append({"token": token["id"], "start": token["overlap_start"],
                      "end": token["overlap_end"]})
    return pins, proof


def _subtract(intervals, cut):
    remaining = []
    for start, end in intervals:
        if end <= cut[0] or start >= cut[1]:
            remaining.append((start, end))
            continue
        if start < cut[0]:
            remaining.append((start, cut[0]))
        if end > cut[1]:
            remaining.append((cut[1], end))
    return remaining


def _presence(event, reviews, pins):
    """每人可证在场/可证离场区间。刷卡时刻按设备误差放宽为区间，
    令牌与监理视频的同场钉定可把误差区间收紧到被证明的时刻。"""
    readers = {r["id"]: r for r in event.get("readers", [])}
    verified = {r["id"] for r in reviews if r["status"] == "verified"}
    swipes = defaultdict(list)
    for swipe in event.get("swipes", []):
        if swipe["id"] in verified:
            swipes[swipe["person"]].append(swipe)
    presence, absence = {}, {}
    for person, records in swipes.items():
        ordered = sorted(records, key=lambda s: (s["local_at"], s["id"]))
        pairs, open_in = [], None
        for swipe in ordered:
            error = readers[swipe["reader"]]["clock_error_min"]
            lo = _shifted(swipe["local_at"], -error)
            hi = _shifted(swipe["local_at"], error)
            if swipe["action"] == "sign_in":
                if open_in is not None:
                    pairs.append((open_in, None))
                open_in = (lo, hi)
            elif swipe["action"] == "sign_out" and open_in is not None:
                pairs.append((open_in, (lo, hi)))
                open_in = None
        if open_in is not None:
            pairs.append((open_in, None))
        proven, gone = [], []
        for index, (sign_in, sign_out) in enumerate(pairs):
            in_lo, in_hi = sign_in
            out_lo, out_hi = sign_out if sign_out else (None, None)
            for pin_start, pin_end in pins.get(person, []):
                start, end = _parse(pin_start), _parse(pin_end)
                if start <= in_hi and end >= in_lo:
                    in_hi = min(in_hi, start)
                if out_lo is not None and start <= out_hi and end >= out_lo:
                    out_lo = max(out_lo, end)
            if index == 0:
                gone.append((EPOCH, in_lo))
            if out_hi is not None:
                gone.append((out_hi, FAR_FUTURE))
            proven.append((in_hi, out_lo if out_lo is not None else FAR_FUTURE))
        presence[person] = proven
        absence[person] = gone
    for record in event.get("absences", []):
        person = record["person"]
        cut = (_parse(record["start"]), _parse(record["end"]))
        presence[person] = _subtract(presence.get(person, []), cut)
        absence.setdefault(person, []).append(cut)
    return presence, absence


def _qualified(person, start, end, window_end):
    cert = (person or {}).get("cert") or {}
    if cert.get("holder_id") != person["id"]:
        return False
    if REQUIRED_CATEGORY not in cert.get("categories", []):
        return False
    if not cert.get("until") or _parse(cert["until"]) < window_end:
        return False
    return not any(_parse(s["start"]) < end and _parse(s["end"]) > start
                   for s in person.get("suspensions", []))


def _segments(event, people, presence, absence, ws, we):
    points = {ws, we}

    def clip(a, b):
        return max(a, ws), min(b, we)

    for entry in event.get("roster", []):
        a, b = clip(_parse(entry["start"]), _parse(entry["end"]))
        if a < b:
            points.update((a, b))
    for person in people.values():
        pid = person["id"]
        for a, b in presence.get(pid, []) + absence.get(pid, []):
            a, b = clip(a, b)
            if a < b:
                points.update((a, b))
        for sus in person.get("suspensions", []):
            a, b = clip(_parse(sus["start"]), _parse(sus["end"]))
            if a < b:
                points.update((a, b))
    ordered = sorted(points)
    segments = []
    for a, b in zip(ordered, ordered[1:]):
        mid = a + (b - a) / 2
        entry = next((r for r in event.get("roster", [])
                      if _parse(r["start"]) <= mid < _parse(r["end"])), None)
        if entry is None:
            state, commander = "unrostered", None
        else:
            commander = entry["person"]
            present = any(s <= mid < e for s, e in presence.get(commander, []))
            gone = any(s <= mid < e for s, e in absence.get(commander, []))
            if present and _qualified(people.get(commander), a, b, we):
                state = "proven"
            elif present:
                state = "unqualified"
            elif gone:
                state = "absent"
            else:
                state = "unverified"
        segments.append({"start": _fmt(a), "end": _fmt(b),
                         "commander": commander, "state": state})
    merged = []
    for seg in segments:
        if (merged and merged[-1]["commander"] == seg["commander"]
                and merged[-1]["state"] == seg["state"]
                and merged[-1]["end"] == seg["start"]):
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    return merged


def _accepts(order, gap, segments, people, window_end, plan_version):
    """保守换班协议：接班指挥员在空档之后、可证在岗且合格时签发的复工令，
    按当前方案版本接管未证实区间；空档本身仍列为未证实，不追认为已覆盖。"""
    if order.get("plan_version") != plan_version:
        return False
    moment = order.get("at", "")
    if not moment or _parse(moment) <= _parse(gap["end"]):
        return False
    commander = None
    for seg in segments:
        if seg["start"] <= moment < seg["end"] and seg["state"] == "proven":
            commander = seg["commander"]
    if commander != order.get("by"):
        return False
    at = _parse(moment)
    return _qualified(people.get(order["by"]), at, at, window_end)


def commander_at(chain, moment):
    """某一时刻可证在岗的当班指挥员；未证实或空档时刻返回 None。"""
    segments = chain.get("segments", [])
    for seg in segments:
        if seg["start"] <= moment < seg["end"] and seg["state"] == "proven":
            return seg["commander"]
    if segments and moment == segments[-1]["end"] and segments[-1]["state"] == "proven":
        return segments[-1]["commander"]
    return None


def _build_chain(event, window_start, window_end, plan_version):
    ws, we = _parse(window_start), _parse(window_end)
    people = {p["id"]: p for p in event.get("people", [])}
    reviews = _review_swipes(event)
    pins, overlap_proof = _proven_pins(event)
    presence, absence = _presence(event, reviews, pins)
    segments = _segments(event, people, presence, absence, ws, we)
    gaps = []
    for seg in segments:
        if seg["state"] == "proven":
            continue
        if gaps and gaps[-1]["state"] == seg["state"] and gaps[-1]["end"] == seg["start"]:
            gaps[-1]["end"] = seg["end"]
        else:
            gaps.append({"start": seg["start"], "end": seg["end"],
                         "state": seg["state"], "accepted": False})
    for gap in gaps:
        if gap["state"] != "unverified":
            continue
        for order in event.get("resume_orders", []):
            if _accepts(order, gap, segments, people, we, plan_version):
                gap.update(accepted=True, accepted_by=order["by"],
                           accepted_at=order["at"])
                break
    coverage = ("proven" if not gaps else
                "conditional" if all(gap["accepted"] for gap in gaps) else "broken")
    workable = []
    for seg in segments:
        if seg["state"] != "proven":
            continue
        if workable and workable[-1]["end"] == seg["start"]:
            workable[-1]["end"] = seg["end"]
        else:
            workable.append({"start": seg["start"], "end": seg["end"]})
    pending = [{"start": g["start"], "end": g["end"]}
               for g in gaps if g["state"] == "unverified"]
    qualified = {}
    for entry in event.get("roster", []):
        a = max(_parse(entry["start"]), ws)
        b = min(_parse(entry["end"]), we)
        if a >= b:
            continue
        pid = entry["person"]
        qualified[pid] = qualified.get(pid, True) and _qualified(people.get(pid), a, b, we)
    overlap = None
    if overlap_proof:
        overlap = {"start": min(p["start"] for p in overlap_proof),
                   "end": max(p["end"] for p in overlap_proof)}
    status = {"proven": "continuous", "conditional": "conditional",
              "broken": "broken"}[coverage]
    return {
        "event": event, "segments": segments, "gaps": gaps, "coverage": coverage,
        "overlap": overlap, "overlap_proven": bool(overlap_proof),
        "pending": pending, "workable": workable, "swipe_reviews": reviews,
        "qualified": qualified, "status": status,
        "decisions": {
            "值守交接链核验": ({"proven": "连续", "conditional": "附条件",
                              "broken": "中断"}[coverage],
                            "两台离线刷卡机签名、设备时钟误差、交接令牌与监理视频钉定的职责链"),
            "交接重叠证明": ("可证重叠" if overlap_proof else "待核",
                            "双方签名的交接令牌与监理签章视频索引证明的同场时段"),
            "刷卡采录验签": ("通过" if all(r["status"] == "verified" for r in reviews) else "异常",
                            "刷卡签名核验、设备序号重放识别与补传标记"),
            "当班资质核验": ("合格" if qualified and all(qualified.values()) else "失格",
                            "证照持有人、类别、有效期与暂停公告对值守段的覆盖"),
        },
    }


def evaluate(events, plans):
    grouped = defaultdict(list)
    for event in events:
        grouped[event["job_id"]].append(event)
    result = {}
    for job, items in grouped.items():
        event = sorted(items, key=lambda e: (int(e["version"]), e.get("effective_at", ""), e["id"]))[-1]
        plan = plans.get(job)
        if plan:
            window_start = plan["event"]["window_start"]
            window_end = plan["event"]["window_end"]
            plan_version = plan["event"]["version"]
        else:
            window_start = event.get("window_start")
            window_end = event.get("window_end")
            plan_version = event.get("plan_version")
        result[job] = _build_chain(event, window_start, window_end, plan_version)
    return result
