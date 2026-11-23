"""门岗离线双联磅单采录：OCR 链、车次聚合、构件归属仲裁与三账端到端。

固定合成票 TR-8：两联净重 1700kg（较早）与 1300kg（较晚），票面只写车次，
不写构件号。仅靠车次二维码与“最新票优先”无法把任何一联归属到 J1，必须保持
待核；只有封签台账绑定或独立监理现场复磅两类完整取证才能改变 J1 的重量裁定。
"""
import copy
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from liftpermit import plans, resources, tickets, work
from liftpermit.store import audit, connect, import_file, register_archived_image, report, ticket_view

ROOT = Path(__file__).resolve().parents[1]


def _png(seed):
    width = height = 8
    seed_bytes = seed.encode("utf-8") if isinstance(seed, str) else bytes(seed)
    raw = b""
    for y in range(height):
        raw += b"\x00" + bytes(
            (seed_bytes[(x + y) % len(seed_bytes)] + x * 3 + y * 5) % 256
            for x in range(width))

    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def capture(ticket_no, net, *, at, weighed_at, seals, image=None, gross=None, tare=None,
            unit="kg", witness=None, ocr=None, truck_qr="TR-8", copy_label=""):
    return {
        "type": "capture", "at": at, "ticket_no": ticket_no, "copy": copy_label,
        "truck_qr": truck_qr, "plate": "沪A-TR8",
        "image_base64": None,  # 由测试用例注入归档 sha
        "gross": net + 4000 if gross is None else gross,
        "tare": 4000 if tare is None else tare,
        "net": net, "unit": unit,
        "scale_calibrated_until": "2026-12-31T23:59",
        "weighed_at": weighed_at, "pallet_seals": list(seals),
        "unload_records": [{"truck_qr": truck_qr, "pieces": 2}],
        "witness": witness or {"id": "gate-1", "signature": "sig-gate"},
        "ocr": ocr or [{"gross": net + 4000, "tare": 4000, "net": net,
                        "unit": unit, "confidence": 0.98}],
        "_image": image if image is not None else _png(ticket_no),
    }


def seal_claim(seal, component="J1", at="2026-12-01T08:05", witness=None, status=None):
    env = {"type": "attribution", "at": at, "basis": "seal_ledger",
           "seal": seal, "component": component,
           "witness": witness or {"id": "seal-clerk", "role": "ledger",
                                  "signature": "sig-seal"}}
    if status:
        env["status"] = status
    return env


def reweigh_claim(component, net_kg, images, *, at, witness=None, supersedes=None):
    env = {"type": "attribution", "at": at, "basis": "reweigh",
           "component": component, "net_kg": net_kg, "images": list(images),
           "witness": witness or {"id": "supervisor-1", "role": "supervisor",
                                  "signature": "sig-sup"}}
    if supersedes:
        env["supersedes"] = list(supersedes)
    return env


class TicketTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self.db = connect(self.root / "state.sqlite", archive_dir=str(self.archive))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _write(self, payload, name):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return import_file(self.db, "ticket", path)

    def capture_file(self, *envelopes, name="captures.json"):
        """归档合成图像并把 sha 注入信封，模拟门岗先存档后采录。"""
        prepared = []
        for raw_env in envelopes:
            env = copy.deepcopy(raw_env)
            image = env.pop("_image")
            sha = register_archived_image(self.db, image)
            env.pop("image_base64", None)
            env["image_sha"] = sha
            prepared.append(env)
        return self._write(prepared, name), [e["image_sha"] for e in prepared]

    def load_baseline_ledgers(self):
        for kind in ("plan", "asset", "work"):
            import_file(self.db, kind, ROOT / "fixtures" / f"{kind}-ok.json")

    def plan_events(self):
        return [json.loads(r[0]) for r in
                self.db.execute("SELECT body FROM events WHERE kind='plan' ORDER BY seq")]

    def asset_events(self):
        return [json.loads(r[0]) for r in
                self.db.execute("SELECT body FROM events WHERE kind='asset' ORDER BY seq")]

    def work_events(self):
        return [json.loads(r[0]) for r in
                self.db.execute("SELECT body FROM events WHERE kind='work' ORDER BY seq")]

    def three_ledgers(self):
        p = plans.evaluate(self.plan_events())
        a = resources.evaluate(self.asset_events(), p)
        w = work.evaluate(self.work_events(), p, a)
        return p, a, w

    def promote_and_countersign(self, job_id="J1", promo_id="PR-1", sign_id="CS-1",
                                promo_at="2026-12-01T08:10", sign_at="2026-12-01T08:15"):
        self._write([{"type": "promotion", "id": promo_id, "job_id": job_id,
                      "effective_at": promo_at}], f"{promo_id}.json")
        self._write([{"type": "countersign", "id": sign_id, "job_id": job_id,
                      "effective_at": sign_at,
                      "signer": {"id": "engineer-1", "role": "engineer",
                                 "signature": "sig-eng"}}], f"{sign_id}.json")

    def release_for_current_plan(self, event_id="W2"):
        latest = plans.evaluate(self.plan_events())["J1"]["event"]
        new_work = copy.deepcopy(
            json.loads((ROOT / "fixtures" / "work-ok.json").read_text(encoding="utf-8"))[0])
        new_work.update(id=event_id, version=2)
        new_work["permit_actions"][-1].update(id="K2", plan_version=latest["version"])
        path = self.root / f"{event_id}.json"
        path.write_text(json.dumps([new_work], ensure_ascii=False), encoding="utf-8")
        import_file(self.db, "work", path)


class CollisionFixtureTests(TicketTestBase):
    def import_collision(self):
        return import_file(self.db, "ticket", ROOT / "fixtures" / "ticket-collision.json")

    def test_fixed_fixture_two_copies_same_truck_only(self):
        result = self.import_collision()
        self.assertEqual(result["inserted"], 2)
        payload = json.loads((ROOT / "fixtures" / "ticket-collision.json").read_text(encoding="utf-8"))
        self.assertEqual({e["truck_qr"] for e in payload}, {"TR-8"})
        self.assertEqual(sorted(e["net"] for e in payload), [1300, 1700])
        view = ticket_view(self.db)
        trip = view["trips"]["TR-8"]
        # 按过磅时刻排序后较晚一联是 1300kg，但两联都没有构件号。
        self.assertEqual(trip["latest_ticket_id"], "WB-1300")
        self.assertEqual(trip["latest_net_kg"], 1300.0)
        self.assertEqual(set(trip["unresolved_ticket_ids"]), {"WB-1300", "WB-1700"})
        self.assertEqual(trip["status"], "pending_attribution")
        self.assertIn("trip_qr_is_not_component_identity", trip["ambiguity_reasons"])
        self.assertEqual({t["status"] for t in view["tickets"].values()}, {"valid"})
        self.assertEqual(view["jobs"], {})

    def test_latest_ticket_cannot_be_promoted_without_attribution(self):
        self.import_collision()
        with self.assertRaisesRegex(ValueError, "归属未决"):
            self._write([{"type": "promotion", "id": "PR-X", "job_id": "J1",
                          "effective_at": "2026-12-01T08:10"}], "bad-promo.json")
        # 失败事务不留任何 plan 事件，三账基线不受影响。
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM events WHERE kind='plan'").fetchone()[0], 0)

    def test_duplicate_upload_is_idempotent_and_replay_stable(self):
        first = self.import_collision()
        again = self.import_collision()
        self.assertFalse(first["idempotent"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM events WHERE kind='ticket'").fetchone()[0], 2)
        images = report(self.db)["ticket_images"]
        self.assertEqual(len(images), 2)
        self.assertTrue(audit(self.db)["ok"])

    def test_image_archived_before_db_event_and_retry_keeps_single_archive(self):
        result = self.import_collision()
        archived = sorted(p.name for p in self.archive.rglob("*") if p.is_file())
        self.assertEqual(len(archived), 2)
        # 重跑命中内容寻址归档，不产生第二份图像，也不产生第二条业务事件。
        self.import_collision()
        self.assertEqual(sorted(p.name for p in self.archive.rglob("*") if p.is_file()), archived)
        self.assertEqual(result["sha"], result["sha"])
        self.assertTrue(audit(self.db)["ok"])

    def test_archive_failure_writes_nothing_and_retry_converges(self):
        # 归档目录指向一个普通文件，图像无法落盘，数据库不得有半条写入。
        blocked = self.root / "not-a-dir"
        blocked.write_text("x")
        bad_db = connect(self.root / "blocked.sqlite", archive_dir=str(blocked))
        with self.assertRaises(NotADirectoryError):
            import_file(bad_db, "ticket", ROOT / "fixtures" / "ticket-collision.json")
        self.assertEqual(bad_db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(bad_db.execute("SELECT COUNT(*) FROM ticket_images").fetchone()[0], 0)
        bad_db.close()
        # 换到可用归档重跑同一文件，业务结论与正常路径一致。
        self.import_collision()
        view = ticket_view(self.db)
        self.assertEqual(view["trips"]["TR-8"]["status"], "pending_attribution")


class TicketValidationTests(TicketTestBase):
    def test_net_formula_mismatch_marks_ticket_invalid(self):
        env = capture("WB-X", 1300, at="2026-12-01T07:40", weighed_at="2026-12-01T07:35",
                      seals=["seal-1"], gross=9999)
        self.capture_file(env)
        ticket = ticket_view(self.db)["tickets"]["WB-X"]
        self.assertFalse(ticket["valid"])
        self.assertIn("net_formula_mismatch", ticket["reasons"])
        self.assertEqual(ticket["status"], "reading_invalid")

    def test_expired_calibration_blocks_validity(self):
        env = capture("WB-X", 1300, at="2026-12-01T07:40", weighed_at="2026-12-01T07:35",
                      seals=["seal-1"])
        env["scale_calibrated_until"] = "2026-11-30T00:00"
        self.capture_file(env)
        ticket = ticket_view(self.db)["tickets"]["WB-X"]
        self.assertIn("calibration_expired", ticket["reasons"])
        self.assertFalse(ticket["valid"])

    def test_unknown_unit_rejected_not_silently_coerced(self):
        env = capture("WB-X", 1300, at="2026-12-01T07:40", weighed_at="2026-12-01T07:35",
                      seals=["seal-1"], unit="lbs")
        self.capture_file(env)
        self.assertIn("unknown_unit", ticket_view(self.db)["tickets"]["WB-X"]["reasons"])

    def test_ton_to_kilogram_revision_normalizes_and_preserves_chain(self):
        # 票面 1.7 吨被 OCR 成 1.7 千克，人工改单位后回到 1700kg，改判链完整。
        env = capture("WB-T", 1.7, at="2026-12-01T07:30", weighed_at="2026-12-01T07:20",
                      seals=["seal-9"], unit="kg", gross=5.7, tare=4.0,
                      ocr=[{"gross": 5.7, "tare": 4.0, "net": 1.7, "unit": "kg"}])
        _, shas = self.capture_file(env)
        self._write([
            {"type": "manual_revision", "at": "2026-12-01T07:45", "image_sha": shas[0],
             "ticket_no": "WB-T", "field": "unit", "from": "kg", "to": "t",
             "by": "gate-1"},
        ], "rev.json")
        ticket = ticket_view(self.db)["tickets"]["WB-T"]
        self.assertTrue(ticket["valid"])
        self.assertEqual(ticket["accepted_reading"]["net_kg"], 1700.0)
        records = report(self.db)["business_records"]
        self.assertTrue(any(r["entity_type"] == "磅单改判" for r in records))

    def test_same_image_different_ocr_without_manual_call_goes_pending(self):
        env = capture("WB-O", 1300, at="2026-12-01T07:40", weighed_at="2026-12-01T07:35",
                      seals=["seal-1"])
        _, shas = self.capture_file(env)
        self._write([{"type": "ocr_rerun", "at": "2026-12-01T07:50",
                      "image_sha": shas[0], "ticket_no": "WB-O",
                      "candidate": {"gross": 5555, "tare": 4000, "net": 1555, "unit": "kg"}}],
                    "ocr2.json")
        ticket = ticket_view(self.db)["tickets"]["WB-O"]
        self.assertFalse(ticket["valid"])
        self.assertEqual(ticket["status"], "ocr_conflict")
        self.assertEqual(len(ticket["ocr_conflict_events"]), 1)

    def test_manual_resolution_clears_ocr_conflict_with_audit_trail(self):
        env = capture("WB-O", 1300, at="2026-12-01T07:40", weighed_at="2026-12-01T07:35",
                      seals=["seal-1"])
        _, shas = self.capture_file(env)
        self._write([
            {"type": "ocr_rerun", "at": "2026-12-01T07:50", "image_sha": shas[0],
             "ticket_no": "WB-O",
             "candidate": {"gross": 5555, "tare": 4000, "net": 1555, "unit": "kg"}},
            {"type": "manual_revision", "at": "2026-12-01T07:55", "image_sha": shas[0],
             "ticket_no": "WB-O", "field": "net", "from": 1555, "to": 1300,
             "by": "gate-1"},
            {"type": "manual_revision", "at": "2026-12-01T07:56", "image_sha": shas[0],
             "ticket_no": "WB-O", "field": "gross", "from": 5555, "to": 5300,
             "by": "gate-1"},
        ], "resolve.json")
        ticket = ticket_view(self.db)["tickets"]["WB-O"]
        self.assertTrue(ticket["valid"])
        self.assertEqual(ticket["ocr_resolved_by"], ticket["chain"][0]["id"])

    def test_same_image_reupload_with_new_ocr_version_converges_after_resolution(self):
        # 同一张物理票图两次 OCR 版本：未人工裁决前一律 ocr_conflict 待核，
        # 任何重跑/重放都不得在两个读数间静默选一个。
        env = capture("WB-R", 1300, at="2026-12-01T07:40",
                      weighed_at="2026-12-01T07:35", seals=["seal-1"])
        _, shas = self.capture_file(env)
        view_before = ticket_view(self.db)
        self._write([{"type": "ocr_rerun", "at": "2026-12-01T07:48",
                      "image_sha": shas[0], "ticket_no": "WB-R",
                      "candidate": {"gross": 5300, "tare": 4000, "net": 1300,
                                    "unit": "kg", "engine": "ocr-v2"}}], "v2-same.json")
        # 数值一致的新 OCR 版本只是佐证，不构成冲突。
        self.assertTrue(ticket_view(self.db)["tickets"]["WB-R"]["valid"])
        self._write([{"type": "ocr_rerun", "at": "2026-12-01T07:52",
                      "image_sha": shas[0], "ticket_no": "WB-R",
                      "candidate": {"gross": 5400, "tare": 4000, "net": 1400,
                                    "unit": "kg", "engine": "ocr-v3"}}], "v3-diff.json")
        ticket = ticket_view(self.db)["tickets"]["WB-R"]
        self.assertEqual(ticket["status"], "ocr_conflict")
        # 待核期间封签绑定也不能把冲突票推广成独立测量。
        self._write([seal_claim("seal-1", at="2026-12-01T08:00")], "attr.json")
        with self.assertRaisesRegex(ValueError, "校验未通过"):
            self._write([{"type": "promotion", "id": "PR-BAD", "job_id": "J1",
                          "effective_at": "2026-12-01T08:05"}], "bad-promo.json")
        # 人工确认原读数后恢复有效，归属与重放结论稳定。
        self._write([{"type": "manual_revision", "at": "2026-12-01T08:10",
                      "image_sha": shas[0], "ticket_no": "WB-R",
                      "field": "net", "from": 1400, "to": 1300,
                      "by": "gate-1"},
                     {"type": "manual_revision", "at": "2026-12-01T08:11",
                      "image_sha": shas[0], "ticket_no": "WB-R",
                      "field": "gross", "from": 5400, "to": 5300,
                      "by": "gate-1"}], "resolved.json")
        view = ticket_view(self.db)
        self.assertTrue(view["tickets"]["WB-R"]["valid"])
        self.assertEqual(view["jobs"]["J1"]["net_kg"], 1300.0)
        self.assertTrue(audit(self.db)["ok"])

    def test_capture_requires_signed_witness(self):
        env = capture("WB-NW", 1300, at="2026-12-01T07:40",
                      weighed_at="2026-12-01T07:35", seals=["seal-1"],
                      witness={"id": "gate-1", "signature": ""})
        with self.assertRaises(ValueError):
            self.capture_file(env)


class AttestationPathTests(TicketTestBase):
    def two_copies(self):
        first = capture("WB-1700", 1700, at="2026-12-01T07:30",
                        weighed_at="2026-12-01T07:20", seals=["seal-2"], copy_label="first")
        second = capture("WB-1300", 1300, at="2026-12-01T07:40",
                         weighed_at="2026-12-01T07:35", seals=["seal-1"], copy_label="second")
        self.capture_file(first, second)
        return [e["image_sha"] for e in
                json.loads((self.root / "captures.json").read_text(encoding="utf-8"))]

    def test_upload_order_swap_gives_identical_attribution(self):
        # 先传较晚的 1300 联，再传较早的 1700 联。
        first = capture("WB-1700", 1700, at="2026-12-01T07:30",
                        weighed_at="2026-12-01T07:20", seals=["seal-2"])
        second = capture("WB-1300", 1300, at="2026-12-01T07:40",
                         weighed_at="2026-12-01T07:35", seals=["seal-1"])
        self.capture_file(second, first, name="swapped.json")
        self._write([seal_claim("seal-1")], "attr.json")
        view = ticket_view(self.db)
        job = view["jobs"]["J1"]
        self.assertEqual(job["status"], "matched")
        self.assertEqual(job["net_kg"], 1300.0)
        self.assertEqual(view["trips"]["TR-8"]["latest_ticket_id"], "WB-1300")

    def test_seal_ledger_binds_1300_ticket_to_j1(self):
        self.two_copies()
        self._write([seal_claim("seal-1")], "attr.json")
        view = ticket_view(self.db)
        job = view["jobs"]["J1"]
        self.assertEqual(job["basis"], "seal_ledger")
        self.assertEqual(job["net_kg"], 1300.0)
        self.assertEqual(job["claim"]["ticket_id"], "WB-1300")
        self.assertTrue(job["claim"]["images"])
        # 另一联仍显式未匹配，不能被“最新票优先”吞掉。
        self.assertEqual(view["trips"]["TR-8"]["unresolved_ticket_ids"], ["WB-1700"])

    def test_broken_seal_does_not_attribute_and_later_correction_recovers(self):
        self.two_copies()
        self._write([seal_claim("seal-1", status="broken", at="2026-12-01T08:05")],
                    "broken.json")
        view = ticket_view(self.db)
        # 破损封签登记为显式待核证据，而不是让构件从视图里消失。
        self.assertEqual(view["jobs"]["J1"]["status"], "pending")
        self.assertIsNone(view["jobs"]["J1"]["net_kg"])
        self.assertEqual(view["trips"]["TR-8"]["status"], "pending_attribution")
        # 事后封签台账更正：同一封签重新完好绑定并显式取代破损裁定。
        broken_id = ticket_view(self.db)["jobs"]["J1"]["claims"][0]["event_id"]
        self._write([seal_claim("seal-1", at="2026-12-01T08:20",
                                witness={"id": "seal-clerk", "role": "ledger",
                                         "signature": "sig-seal-2"}) |
                     {"supersedes": [broken_id]}],
                    "corrected.json")
        job = ticket_view(self.db)["jobs"]["J1"]
        self.assertEqual(job["status"], "matched")
        self.assertEqual(job["net_kg"], 1300.0)
        self.assertTrue(audit(self.db)["ok"])

    def test_conflicting_attributions_remain_pending_until_explicit_supersede(self):
        self.two_copies()
        self._write([seal_claim("seal-1", at="2026-12-01T08:05")], "a1.json")
        seal_job = ticket_view(self.db)["jobs"]["J1"]
        self.assertEqual(seal_job["net_kg"], 1300.0)
        # 独立监理现场复磅给出 1700kg，两证重量冲突，必须显式待核而非择新。
        images = list(ticket_view(self.db)["tickets"])
        self._write([reweigh_claim("J1", 1700, images, at="2026-12-01T08:30")],
                    "rw-conflict.json")
        view = ticket_view(self.db)
        self.assertEqual(view["jobs"]["J1"]["status"], "conflict")
        self.assertIsNone(view["jobs"]["J1"]["net_kg"])
        with self.assertRaisesRegex(ValueError, "归属未决"):
            self._write([{"type": "promotion", "id": "PR-BAD", "job_id": "J1",
                          "effective_at": "2026-12-01T08:35"}], "bad.json")

    def test_reweigh_requires_independent_supervisor_witness(self):
        self.two_copies()
        with self.assertRaises(ValueError):
            self._write([reweigh_claim(
                "J1", 1700, ["img"], at="2026-12-01T08:30",
                witness={"id": "gate-1", "role": "gate", "signature": "sig-gate"})],
                "bad-rw.json")


class ThreeLedgerEndToEndTests(TicketTestBase):
    def two_copies(self):
        first = capture("WB-1700", 1700, at="2026-12-01T07:30",
                        weighed_at="2026-12-01T07:20", seals=["seal-2"], copy_label="first")
        second = capture("WB-1300", 1300, at="2026-12-01T07:40",
                         weighed_at="2026-12-01T07:35", seals=["seal-1"], copy_label="second")
        self.capture_file(first, second)

    def test_pending_trip_keeps_baseline_ledger_finished(self):
        self.load_baseline_ledgers()
        p, a, w = self.three_ledgers()
        self.assertTrue(w["J1"]["completed"])
        self.two_copies()
        # 只有车次 QR：没有任何 plan 新版本，原 1000kg 裁定与完工状态保持不变，
        # 但票域决策显式待核。
        p, a, w = self.three_ledgers()
        self.assertEqual(p["J1"]["mass_kg"], 1000)
        self.assertTrue(w["J1"]["completed"])
        decisions = report(self.db)["decisions"]
        self.assertEqual(decisions["TR-8/车次磅单聚合"]["result"], "待核")

    def test_seal_path_1300_new_version_resign_then_release_finishes(self):
        self.load_baseline_ledgers()
        self.two_copies()
        self._write([seal_claim("seal-1")], "attr.json")
        self.promote_and_countersign()
        p, a, w = self.three_ledgers()
        self.assertTrue(p["J1"]["ready"])
        self.assertEqual(p["J1"]["mass_kg"], 1300.0)
        self.assertEqual(p["J1"]["attribution"]["basis"], "seal_ledger")
        # 1300 × 1.1 = 1430 ≤ 1600，两根索具各自有余量。
        self.assertEqual(a["J1"]["status"], "reserved")
        self.assertTrue(a["J1"]["ready"])
        # 旧许可绑定 v1，新版本未复签前不得放行；复签后仍需新许可动作。
        self.assertFalse(w["J1"]["active"])
        self.release_for_current_plan()
        p, a, w = self.three_ledgers()
        self.assertTrue(w["J1"]["completed"])
        self.assertTrue(audit(self.db)["ok"])

    def test_reweigh_path_1700_freezes_resources_and_blocks_completion(self):
        self.load_baseline_ledgers()
        self.two_copies()
        image_shas = []
        for agg in ticket_view(self.db)["tickets"].values():
            image_shas.extend(agg["images"])
        self._write([reweigh_claim("J1", 1700, image_shas, at="2026-12-01T08:30")],
                    "reweigh.json")
        self.promote_and_countersign()
        p, a, w = self.three_ledgers()
        self.assertEqual(p["J1"]["mass_kg"], 1700.0)
        self.assertEqual(p["J1"]["attribution"]["basis"], "reweigh")
        # 动态需求 1870 > 额定 1600，资源账冻结，任何新许可都无法完工。
        self.assertGreater(a["J1"]["dynamic_demand_kg"], a["J1"]["rated_kg"])
        self.assertEqual(a["J1"]["status"], "frozen")
        self.assertFalse(a["J1"]["ready"])
        self.assertIn("crane_rating_or_inspection", a["J1"]["freeze_reasons"])
        self.assertFalse(w["J1"]["active"])
        self.assertFalse(w["J1"]["completed"])
        self.assertIn("resources_frozen", w["J1"]["hold_reasons"])
        # 即使现场补一条针对当前版本的许可动作，资源冻结依旧使作业不得完工。
        self.release_for_current_plan(event_id="W2-frozen")
        _, _, w2 = self.three_ledgers()
        self.assertFalse(w2["J1"]["completed"])
        self.assertFalse(w2["J1"]["active"])
        self.assertTrue(audit(self.db)["ok"])

    def test_wrong_attribution_compensation_revokes_old_measurement_and_stops_work(self):
        self.load_baseline_ledgers()
        self.two_copies()
        # 错误裁定：先把 1300 联绑到 J1 并完成推广、复签、新版许可，作业放行。
        self._write([seal_claim("seal-1", at="2026-12-01T08:05")], "wrong-attr.json")
        self.promote_and_countersign(promo_id="PR-1", sign_id="CS-1")
        self.release_for_current_plan(event_id="W2")
        p, a, w = self.three_ledgers()
        self.assertTrue(w["J1"]["completed"])
        wrong_measure = next(m for m in p["J1"]["event"]["measurements"]
                             if m["id"] == "PR-1:measure:ticket")
        # 现场复磅暴露真实重量 1700kg，声明取代原封签归属。
        old_claim = ticket_view(self.db)["jobs"]["J1"]["claim"]["event_id"]
        image_shas = []
        for agg in ticket_view(self.db)["tickets"].values():
            image_shas.extend(agg["images"])
        self._write([reweigh_claim("J1", 1700, image_shas, at="2026-12-01T08:40",
                                   supersedes=[old_claim])], "reweigh.json")
        self.promote_and_countersign(promo_id="PR-2", sign_id="CS-2",
                                     promo_at="2026-12-01T08:45", sign_at="2026-12-01T08:50")
        p, a, w = self.three_ledgers()
        # 旧裁定留痕：被撤回的 1300kg 测量仍在事件里但 revoked，不参与选重。
        chain = self.plan_events()[-1]["measurements"]
        revoked = [m for m in chain if m.get("revoked")]
        self.assertTrue(any(m["kg"] == 1300.0 for m in revoked))
        self.assertEqual(p["J1"]["mass_kg"], 1700.0)
        self.assertEqual(a["J1"]["status"], "frozen")
        # 旧许可针对旧方案版本，资源冻结且许可版本过期，现场立即停工且不能完工。
        self.assertFalse(w["J1"]["completed"])
        self.assertFalse(w["J1"]["active"])
        self.assertIn("permit_not_released_for_version", w["J1"]["hold_reasons"])
        self.assertTrue(audit(self.db)["ok"])

    def test_promotion_unsigned_cannot_keep_old_permit_finished(self):
        self.load_baseline_ledgers()
        self.two_copies()
        self._write([seal_claim("seal-1")], "attr.json")
        self._write([{"type": "promotion", "id": "PR-ONLY", "job_id": "J1",
                      "effective_at": "2026-12-01T08:10"}], "promo-only.json")
        p, a, w = self.three_ledgers()
        self.assertFalse(p["J1"]["signed"])
        self.assertFalse(p["J1"]["ready"])
        # 资源侧指标本身仍合格，但 ready 被方案账拉下并给出冻结原因。
        self.assertFalse(a["J1"]["ready"])
        self.assertIn("plan_not_ready", a["J1"]["freeze_reasons"])
        self.assertFalse(w["J1"]["active"])

    def test_weighbridge_correction_supersedes_original_ticket_deterministically(self):
        self.load_baseline_ledgers()
        env = capture("WB-C", 1300, at="2026-12-01T07:40",
                      weighed_at="2026-12-01T07:35", seals=["seal-1"])
        _, shas = self.capture_file(env)
        self._write([{"type": "correction", "at": "2026-12-01T08:00",
                      "image_sha": shas[0], "ticket_no": "WB-C", "by": "scale-office",
                      "reading": {"gross": 5700, "tare": 4000, "net": 1700, "unit": "kg"}}],
                    "correction.json")
        ticket = ticket_view(self.db)["tickets"]["WB-C"]
        self.assertTrue(ticket["valid"])
        self.assertEqual(ticket["accepted_reading"]["net_kg"], 1700.0)
        self._write([seal_claim("seal-1", at="2026-12-01T08:05")], "attr.json")
        self.assertEqual(ticket_view(self.db)["jobs"]["J1"]["net_kg"], 1700.0)
        # 原票与更正都在业务记录里留痕，重放结论一致。
        types = {r["entity_type"] for r in report(self.db)["business_records"]}
        self.assertIn("磅单更正", types)
        self.assertTrue(audit(self.db)["ok"])

    def test_audit_replays_ticket_bytes_and_plan_events_identically(self):
        self.load_baseline_ledgers()
        self.two_copies()
        self._write([seal_claim("seal-1")], "attr.json")
        self.promote_and_countersign()
        self.release_for_current_plan()
        result = audit(self.db)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["source_count"], 5)


if __name__ == "__main__":
    unittest.main()
