"""Offline duplicate-copy weighbridge ticket capture and component attribution.

门岗离线采录的领域模型，不触碰任何真实设备。一张车次磅单（双联中的一联）
经历以下只增不改的事件链（全部以 kind="ticket" 进入同一个事件库）：

capture        一张图一次采录：原始字节、OCR 候选、人工修订、毛/皮/净重、
               计量单位、设备校准、过磅时刻、托盘封签、拆车记录、见证签字
ocr_rerun      同一张图重跑 OCR，保留候选；与当前采纳值不一致且未经人工裁决
               时只挂 ocr_conflict，绝不静默改判
manual_revision 人工改字，逐字段记录 from/to，形成改判链
correction     磅房事后更正，整读替换并保留原票
attribution    实体归属证据：封签台账（seal_ledger）或独立监理现场复磅（reweigh）

二维码只指向车次（truck_qr），因此车次聚合永远不能直接写成某构件的 independent
measurement。evaluate 只产出“每联票 -> 构件”的归属证据与未决歧义；真正写入方案账
须经 plans 侧的独立测量规则与新版本签认（见 liftpermit.store 的 promotion/countersign）。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key

UNIT_TO_KG = {
    "kg": Decimal("1"), "千克": Decimal("1"), "kilogram": Decimal("1"),
    "t": Decimal("1000"), "ton": Decimal("1000"), "吨": Decimal("1000"),
}

# 允许把票归到具体构件的两类完整取证；仅有车次 QR 不在其中。
ATTRIBUTION_BASES = ("seal_ledger", "reweigh")
SUPERVISOR_ROLES = ("supervisor", "monitor", "监理")


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def eid(prefix, obj):
    """Content-addressed event id，保证重放/换序上传得到同一业务标识。"""
    return f"{prefix}-{hashlib.sha256(canon(obj).encode('utf-8')).hexdigest()[:16]}"


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def normalize_reading(gross, tare, net, unit, calibrated_until, weighed_at):
    """归一化毛/皮/净重到千克，并校验净重算式与设备校准。

    返回字段全部可序列化；任何一项不成立都给出明确 reason，而不是抛异常，
    让调用方决定该票是作废还是待核。
    """
    factor = UNIT_TO_KG.get(unit.lower() if isinstance(unit, str) else unit)
    raw_gross, raw_tare, raw_net = _decimal(gross), _decimal(tare), _decimal(net)
    reasons = []
    if factor is None:
        reasons.append("unknown_unit")
    if raw_gross is None or raw_tare is None or raw_net is None:
        reasons.append("non_numeric_reading")
    net_check = False
    gross_kg = tare_kg = net_kg = None
    if factor is not None and None not in (raw_gross, raw_tare, raw_net):
        net_check = (raw_gross - raw_tare) == raw_net
        if not net_check:
            reasons.append("net_formula_mismatch")
        gross_kg = float(raw_gross * factor)
        tare_kg = float(raw_tare * factor)
        net_kg = float(raw_net * factor)
    calibration_ok = bool(calibrated_until and weighed_at
                          and str(calibrated_until) >= str(weighed_at))
    if not calibration_ok:
        reasons.append("calibration_expired")
    return {
        "gross": None if raw_gross is None else float(raw_gross),
        "tare": None if raw_tare is None else float(raw_tare),
        "net": None if raw_net is None else float(raw_net),
        "unit_original": unit,
        "gross_kg": gross_kg, "tare_kg": tare_kg, "net_kg": net_kg,
        "net_check_ok": net_check, "calibration_ok": calibration_ok,
        "valid": net_check and calibration_ok and not reasons,
        "reasons": reasons,
    }


def _reading_from_chain(base, chain):
    """从 capture 原始读值出发，依次套用人工修订/磅房更正。"""
    reading = dict(base)
    for event in chain:
        if event["type"] == "manual_revision":
            reading[event["field"]] = event["to"]
        elif event["type"] == "correction":
            reading.update(event["reading"])
    return reading


def build_capture_event(envelope):
    """把采录信封规范化为一个 capture 事件（纯函数，便于测试与重放）。"""
    required = ("at", "truck_qr", "gross", "tare", "net", "unit",
                "scale_calibrated_until", "weighed_at", "witness")
    missing = [k for k in required if k not in envelope]
    if missing:
        raise ValueError(f"capture envelope missing fields: {','.join(missing)}")
    witness = envelope["witness"]
    if not isinstance(witness, dict) or not witness.get("id") or not witness.get("signature"):
        raise ValueError("capture requires signed witness id and signature")
    image_sha = envelope.get("image_sha")
    if not image_sha:
        raise ValueError("capture requires image_sha (archive image first)")
    body = {
        "type": "capture",
        "at": envelope["at"],
        "image_sha": image_sha,
        "image_name": envelope.get("image_name", ""),
        "ticket_no": envelope.get("ticket_no", image_sha),
        "copy": envelope.get("copy", ""),
        "truck_qr": envelope["truck_qr"],
        "plate": envelope.get("plate", ""),
        "ocr": envelope.get("ocr", []),
        "manual_revisions": envelope.get("manual_revisions", []),
        "reading_base": {
            "gross": envelope["gross"], "tare": envelope["tare"],
            "net": envelope["net"], "unit": envelope["unit"],
        },
        "scale_calibrated_until": envelope["scale_calibrated_until"],
        "weighed_at": envelope["weighed_at"],
        "pallet_seals": list(envelope.get("pallet_seals", [])),
        "unload_records": list(envelope.get("unload_records", [])),
        "witness": witness,
    }
    body["id"] = eid("TC", body)
    body["kind"] = "ticket"
    return body


def build_derived_event(envelope):
    """ocr_rerun / manual_revision / correction / attribution 等只增事件。"""
    etype = envelope.get("type")
    if etype not in ("ocr_rerun", "manual_revision", "correction", "attribution"):
        raise ValueError(f"unsupported ticket envelope type: {etype}")
    if not envelope.get("at"):
        raise ValueError(f"{etype} envelope requires at")
    body = {k: v for k, v in envelope.items() if k != "id"}
    if etype == "attribution":
        if body.get("basis") not in ATTRIBUTION_BASES:
            raise ValueError("attribution basis must be seal_ledger or reweigh")
        witness = body.get("witness")
        if not isinstance(witness, dict) or not witness.get("id") or not witness.get("signature"):
            raise ValueError("attribution requires signed independent witness")
        if body["basis"] == "reweigh":
            if witness.get("role") not in SUPERVISOR_ROLES:
                raise ValueError("reweigh requires independent supervisor witness")
            if not body.get("component") or _decimal(body.get("net_kg")) is None:
                raise ValueError("reweigh requires component and net_kg")
        else:
            if not body.get("seal") or not body.get("component"):
                raise ValueError("seal_ledger attribution requires seal and component")
            body.setdefault("status", "intact")
    elif etype == "manual_revision":
        for field in ("field", "from", "to", "by"):
            if field not in body:
                raise ValueError(f"manual_revision requires {field}")
    elif etype == "correction":
        if not body.get("reading") or not body.get("by"):
            raise ValueError("correction requires reading and by")
    else:  # ocr_rerun
        if not body.get("image_sha") or "candidate" not in body:
            raise ValueError("ocr_rerun requires image_sha and candidate reading")
    body["id"] = eid("TX", body)
    body["kind"] = "ticket"
    return body


def _ticket_id(event):
    return event.get("ticket_no") or event["image_sha"]


def evaluate(events):
    """聚合全部 ticket 事件，输出车次、票联、构件三级归属视图。

    关键约束：任何排序都以内容键（过磅时刻、事件 id）为准，上传顺序不影响
    结论；同图 OCR 不一致、封签破损、多归属冲突都显式标 pending/conflict。
    """
    ticket_events = [e for e in events if e.get("kind") == "ticket"]
    captures = {}          # image_sha -> capture event
    image_to_ticket = {}   # image_sha -> ticket_id
    tickets = {}           # ticket_id -> aggregate

    for event in sorted(ticket_events, key=lambda e: (e.get("at", ""), e["id"])):
        etype = event["type"]
        if etype == "capture":
            sha = event["image_sha"]
            if sha in captures and canon(captures[sha]) != canon(event):
                raise ValueError("same image re-captured with different content")
            captures.setdefault(sha, event)
            tid = _ticket_id(event)
            image_to_ticket.setdefault(sha, tid)
            agg = tickets.setdefault(tid, {
                "ticket_id": tid, "trip_id": event["truck_qr"],
                "images": [], "events": [], "ocr_versions": [],
                "chain": [], "seals": list(event.get("pallet_seals", [])),
                "weighed_at": event["weighed_at"], "plate": event.get("plate", ""),
                "copy": event.get("copy", ""),
            })
            if sha not in agg["images"]:
                agg["images"].append(sha)
            agg["events"].append(event["id"])
            agg["ocr_versions"].append({
                "event_id": event["id"], "at": event["at"],
                "candidate": dict(event["reading_base"]), "origin": "capture",
            })
        else:
            sha = event.get("image_sha")
            tid = event.get("ticket_no") or (image_to_ticket.get(sha) if sha else None)
            if etype in ("ocr_rerun", "manual_revision", "correction") and not tid:
                raise ValueError(f"{etype} references unknown image/ticket")
            if etype == "attribution":
                continue
            agg = tickets[tid]
            agg["events"].append(event["id"])
            if etype == "ocr_rerun":
                agg["ocr_versions"].append({
                    "event_id": event["id"], "at": event["at"],
                    "candidate": dict(event["candidate"]), "origin": "ocr_rerun",
                })
            else:
                agg["chain"].append(event)

    # 采纳读值、OCR 冲突与票据有效性
    for tid, agg in tickets.items():
        capture = next(captures[s] for s in agg["images"])
        accepted_base = _reading_from_chain(capture["reading_base"],
                                            sorted(agg["chain"], key=lambda e: (e["at"], e["id"])))
        reading = normalize_reading(
            accepted_base.get("gross"), accepted_base.get("tare"),
            accepted_base.get("net"), accepted_base.get("unit"),
            capture["scale_calibrated_until"], capture["weighed_at"])
        accepted_cmp = {k: reading.get(k) for k in ("gross", "tare", "net", "unit_original")}
        ocr_conflicts, resolved_by = [], None
        for version in agg["ocr_versions"]:
            cand = version["candidate"]
            cand_norm = normalize_reading(
                cand.get("gross"), cand.get("tare"), cand.get("net"), cand.get("unit"),
                capture["scale_calibrated_until"], capture["weighed_at"])
            cand_cmp = {k: cand_norm.get(k) for k in ("gross", "tare", "net", "unit_original")}
            if cand_cmp != accepted_cmp:
                later_resolution = [e for e in agg["chain"]
                                    if (e["at"], e["id"]) >= (version["at"], version["event_id"])]
                if later_resolution:
                    resolved_by = later_resolution[0]["id"]
                else:
                    ocr_conflicts.append(version["event_id"])
        agg["accepted_reading"] = reading
        agg["ocr_conflict_events"] = ocr_conflicts
        agg["ocr_resolved_by"] = resolved_by
        agg["valid"] = reading["valid"] and not ocr_conflicts
        agg["status"] = ("valid" if agg["valid"] else
                         "ocr_conflict" if ocr_conflicts else "reading_invalid")
        agg["reasons"] = reading["reasons"] + (["ocr_conflict"] if ocr_conflicts else [])

    # 归属证据仲裁
    attributions = [e for e in ticket_events if e["type"] == "attribution"]
    claims_by_component = defaultdict(list)
    for event in sorted(attributions, key=lambda e: (e["at"], e["id"])):
        if event["basis"] == "seal_ledger" and event.get("status") == "broken":
            # 破损封签仍要登记为一条未决证据，不能让该构件从视图里消失。
            claims_by_component[event["component"]].append({
                "event_id": event["id"], "at": event["at"], "basis": "seal_ledger",
                "witness": event["witness"]["id"], "evidence": [event["id"]],
                "superseded": False, "supersedes": list(event.get("supersedes", [])),
                "status": "broken_seal", "reasons": ["seal_broken"],
                "component": event["component"], "seal": event["seal"],
            })
            continue
        claim = {"event_id": event["id"], "at": event["at"],
                 "basis": event["basis"], "witness": event["witness"]["id"],
                 "evidence": [event["id"]], "superseded": False,
                 "supersedes": list(event.get("supersedes", []))}
        if event["basis"] == "seal_ledger":
            # 同一封签历史上破损过，但本事件是事后更正且显式取代破损裁定时有效；
            # 没有取代链的完好绑定不能自动抹掉破损记录。
            broken_claim = next((c for c in claims_by_component.get(event["component"], [])
                                 if c.get("status") == "broken_seal"
                                 and c.get("seal") == event["seal"]), None)
            if broken_claim is not None and broken_claim["event_id"] not in event.get("supersedes", []):
                claim["status"] = "unresolved_broken_seal"
                claim["reasons"] = ["seal_previously_broken_without_supersede"]
                claims_by_component[event["component"]].append(claim)
                continue
            bound = [tid for tid, agg in tickets.items() if event["seal"] in agg["seals"]]
            if not bound:
                claim["status"] = "unmatched_seal"
                claim["reasons"] = ["seal_not_on_any_ticket"]
                claims_by_component[event["component"]].append(claim)
                continue
            tid = bound[0]
            agg = tickets[tid]
            claim.update(ticket_id=tid, component=event["component"],
                         net_kg=agg["accepted_reading"]["net_kg"],
                         images=list(agg["images"]))
            claim["evidence"].extend(agg["events"])
        else:
            claim.update(component=event["component"], net_kg=float(_decimal(event["net_kg"])),
                         images=list(event.get("images", [])),
                         ticket_ids=list(event.get("ticket_ids", [])))
            claim["evidence"].extend(event.get("ticket_ids", []))
        claims_by_component[event["component"]].append(claim)

    jobs = {}
    for component, claims in claims_by_component.items():
        for sup in claims:
            for old in claims:
                if old["event_id"] in sup.get("supersedes", []):
                    old["superseded"] = True
        active = [c for c in claims
                  if not c.get("superseded") and c.get("status") is None]
        masses = {c.get("net_kg") for c in active if c.get("net_kg") is not None}
        if not active:
            status, reasons = "pending", ["no_active_attribution"]
        elif len(masses) > 1:
            status, reasons = "conflict", ["multiple_attribution_masses"]
        else:
            status, reasons = "matched", []
        chosen = sorted(active, key=lambda c: (c["at"], c["event_id"]))[-1] if active else None
        jobs[component] = {
            "job_id": component, "status": status, "reasons": reasons,
            "net_kg": chosen.get("net_kg") if chosen and len(masses) <= 1 else None,
            "basis": chosen["basis"] if chosen and status == "matched" else None,
            "claim": chosen, "claims": claims,
        }

    trips = defaultdict(list)
    for tid, agg in tickets.items():
        trips[agg["trip_id"]].append(agg)
    trip_view = {}
    for trip_id, aggs in trips.items():
        ordered = sorted(aggs, key=lambda a: (a["weighed_at"], a["ticket_id"]))
        latest = ordered[-1]
        attributed_tickets = set()
        for job in jobs.values():
            claim = job.get("claim")
            if claim and claim.get("ticket_id"):
                attributed_tickets.add(claim["ticket_id"])
        unresolved = [a["ticket_id"] for a in ordered
                      if a["valid"] and a["ticket_id"] not in attributed_tickets]
        trip_view[trip_id] = {
            "trip_id": trip_id,
            "ticket_ids": [a["ticket_id"] for a in ordered],
            "latest_ticket_id": latest["ticket_id"],
            "latest_net_kg": latest["accepted_reading"]["net_kg"],
            "unresolved_ticket_ids": unresolved,
            "ambiguity_reasons": (["trip_qr_is_not_component_identity"] if unresolved else []),
            "status": "pending_attribution" if unresolved else "attributed",
        }

    return {"trips": trip_view, "tickets": tickets, "jobs": jobs}


def require_attributed(view, job_id):
    """推广为方案独立测量前的硬门槛：必须有有效票联与完整归属证据。"""
    job = view["jobs"].get(job_id)
    if not job or job["status"] != "matched":
        raise ValueError(
            f"job {job_id} 磅单归属未决，车次二维码不能替代构件实体归属: "
            f"{job['reasons'] if job else ['no_evidence']}")
    claim = job["claim"]
    if claim["basis"] == "seal_ledger":
        ticket = view["tickets"][claim["ticket_id"]]
        if not ticket["valid"]:
            raise ValueError(f"ticket {claim['ticket_id']} 校验未通过: {ticket['reasons']}")
    return job
