"""日历采入与全局分配的离线验收。

覆盖:循环实例展开/取消、系列改单次、取消先于原单、旧 CSV 晚到不复活、
同号跨发行方并存、夏令时折返、贪心反例、两种授权分配协议的分配表对比、
J2 从 resources.ready 到 work 状态的追踪,以及中断后重算得到同一张表。
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from liftpermit import calendar_capture, plans, resources, work
from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "fixtures" / "calendar-contention.json").read_text(encoding="utf-8"))
PACKAGES = FIXTURE["calendars"]
FLEET = {c["id"]: c for c in FIXTURE["fleet"]}

SAME_SHIFT_TABLE = {
    "protocol": "same_shift",
    "assignments": [
        {"job_id": "J1", "crane_id": "TC2", "start": "2026-12-01T09:00",
         "end": "2026-12-01T10:00", "from_crane": "TC1", "moved": True,
         "booking_resign": True},
        {"job_id": "J2", "crane_id": "TC1", "start": "2026-12-01T09:00",
         "end": "2026-12-01T10:00", "from_crane": "TC1", "moved": False,
         "booking_resign": False},
    ],
    "pending": [],
    "objective": {"placed": 2, "moved": 1},
}

PRESERVE_TABLE = {
    "protocol": "preserve_signoff",
    "assignments": [
        {"job_id": "J1", "crane_id": "TC1", "start": "2026-12-01T09:00",
         "end": "2026-12-01T10:00", "from_crane": "TC1", "moved": False,
         "booking_resign": False},
    ],
    "pending": [
        {"job_id": "J2", "status": "待改期", "blocked_by": [
            {"constraint": "capacity", "crane_id": "TC1", "holder": "J1",
             "holder_signed": True,
             "detail": "TC1 在 2026-12-01T09:00 至 2026-12-01T10:00 已被 J1 占用；"
                       "保留签认协议下不可自动转让"},
            {"constraint": "rating", "crane_id": "TC2", "rated_kg": 1300,
             "required_kg": 1430.0,
             "detail": "TC2 额定 1300kg 低于动态需求 1430.0kg"},
        ]},
    ],
    "objective": {"placed": 1, "moved": 0},
}


def sample(name):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))[0]


def j2_plan():
    """J2 的方案事件:同窗 09:00-10:00,独立复测 1300kg(动态需求 1430kg)。"""
    event = copy.deepcopy(sample("plan-ok.json"))
    event.update(id="P-J2", job_id="J2")
    event["measurements"][1] = {"id": "M1-J2", "type": "independent", "kg": 1300,
                                "at": "2026-12-01T07:50", "witness": "surveyor"}
    return event


def j2_asset():
    """J2 的设备事件:班组/索具随工单,请求塔机 TC1(其 CSV 预订)。"""
    return {"id": "A-J2", "kind": "asset", "job_id": "J2", "version": 1,
            "effective_at": "2026-12-01T08:05",
            "window_start": "2026-12-01T09:00", "window_end": "2026-12-01T10:00",
            "reservation_status": "reserved",
            "crane": copy.deepcopy(FLEET["TC1"]),
            "slings": copy.deepcopy(FIXTURE["crews"]["J2"]["slings"]),
            "commander": copy.deepcopy(FIXTURE["crews"]["J2"]["commander"]),
            "competing_reservations": []}


def j2_work():
    event = copy.deepcopy(sample("work-ok.json"))
    event.update(id="W-J2", job_id="J2")
    return event


def plan_accounts():
    return plans.evaluate([sample("plan-ok.json"), j2_plan()])


def dispatch(protocol, version=1):
    return {"id": f"D-{protocol}-v{version}", "kind": "dispatch", "version": version,
            "effective_at": "2026-11-22T09:00", "protocol": protocol,
            "local_tz": FIXTURE["local_tz"],
            "equipment_aliases": FIXTURE["equipment_aliases"],
            "fleet": FIXTURE["fleet"]}


def view(protocol="same_shift", packages=None, accounts=None):
    events = [calendar_capture.calendar_event(p)
              for p in (PACKAGES if packages is None else packages)]
    return calendar_capture.evaluate(events, [dispatch(protocol)],
                                     plan_accounts() if accounts is None else accounts)


def alloc_table(rep):
    """从持久化的冲突图行重建分配表,用于跨库直接比较。"""
    rows = rep["calendar"]
    return {"assignments": [r["body"] for r in rows if r["section"] == "assignment"],
            "pending": [r["body"] for r in rows if r["section"] == "pending"]}


class CalendarCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / "state.sqlite")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def import_event(self, event, kind=None):
        path = Path(self.tmp.name) / f"{event['id']}.json"
        path.write_text(json.dumps([event], ensure_ascii=False), encoding="utf-8")
        return import_file(self.db, kind or event["kind"], path)

    def import_scenario(self, protocol):
        for package in PACKAGES:
            self.import_event(calendar_capture.calendar_event(package), "calendar")
        self.import_event(dispatch(protocol), "dispatch")
        for event in (sample("plan-ok.json"), j2_plan(),
                      sample("asset-ok.json"), j2_asset(),
                      sample("work-ok.json"), j2_work()):
            self.import_event(event)

    def entity(self, subsystem, job):
        for row in report(self.db)["entities"]:
            if row["subsystem"] == subsystem and row["entity_id"] == job:
                return row["state"]
        return None

    # ---------------------------------------------------------- 采入与实例身份

    def test_fixture_j1_window_and_mass_match_plan_ok(self):
        j1 = next(j for j in FIXTURE["jobs"] if j["job_id"] == "J1")
        account = plan_accounts()["J1"]
        self.assertEqual(j1["mass_kg"], 1000)
        self.assertEqual(account["mass_kg"], 1000)
        self.assertEqual((j1["window_start"], j1["window_end"]),
                         (account["event"]["window_start"], account["event"]["window_end"]))

    def test_recurring_series_exdate_override_and_cancellation_tombstone(self):
        instances = [i for i in view()["instances"] if i["series"] == "J1-SERIES"]
        by_local = {i["start_local"]: i for i in instances}
        # 系列 ID 本身不是一次预约:每个实例都有具体起点,且 5 次循环展开为 4 个实例
        self.assertEqual(len(instances), 4)
        self.assertTrue(all(i["key"].count("|") == 2 for i in instances))
        self.assertIn("2026-11-10T09:00", by_local)
        self.assertNotIn("2026-11-17T09:00", by_local)  # EXDATE 排除,不留实例
        moved = by_local["2026-11-24T14:00"]            # 单次改期,窗口取新值
        self.assertTrue(moved["key"].endswith("|2026-11-24T01:00:00+00:00"))  # 身份仍是原实例
        tombstone = by_local["2026-12-08T09:00"]        # 取消墓碑保留并指向旧实例
        self.assertTrue(tombstone["cancelled"])
        contention = by_local["2026-12-01T09:00"]       # 争用实例:已签认、在 TC1
        self.assertTrue(contention["signed"] and contention["crane_id"] == "TC1")

    def test_series_redefined_as_single_instance(self):
        alpha_v1, alpha_v2 = PACKAGES[0], PACKAGES[1]
        first = view(packages=[alpha_v1])
        self.assertEqual(sum(1 for i in first["instances"] if i["series"] == "J5-SERIES"), 3)
        merged = view(packages=[alpha_v1, alpha_v2])
        singles = [i for i in merged["instances"] if i["series"] == "J5-SERIES"]
        self.assertEqual(len(singles), 1)
        self.assertEqual(singles[0]["start_local"], "2026-11-11T13:00")

    def test_same_booking_id_coexists_across_issuers(self):
        bk100 = [i for i in view()["instances"] if i["booking_id"] == "BK-100"]
        self.assertEqual(len(bk100), 2)
        self.assertEqual({i["issuer"] for i in bk100}, {"rental-beta", "rental-gamma"})
        self.assertEqual(len({i["key"] for i in bk100}), 2)
        self.assertTrue(all(not i["cancelled"] for i in bk100))
        self.assertEqual({i["job_id"] for i in bk100}, {"J2", "J4"})

    def test_cancel_before_original_and_late_old_csv_cannot_resurrect(self):
        beta_current, beta_late_old = PACKAGES[2], PACKAGES[3]
        only_cancel = view(packages=[beta_current])
        tomb = [i for i in only_cancel["instances"] if i["booking_id"] == "BK-200"]
        self.assertEqual(len(tomb), 1)
        self.assertTrue(tomb[0]["cancelled"])  # 取消比原单先到,先立墓碑
        merged = view(packages=[beta_current, beta_late_old])
        still = [i for i in merged["instances"] if i["booking_id"] == "BK-200"]
        self.assertEqual(len(still), 1)
        self.assertTrue(still[0]["cancelled"])  # 晚到的旧 CSV 不能复活占用
        alone = view(packages=[beta_late_old])
        live = [i for i in alone["instances"] if i["booking_id"] == "BK-200"]
        self.assertFalse(live[0]["cancelled"])  # 对照:只有旧包时它是活动的

    def test_dst_fold_and_stable_instance_identity(self):
        gamma_ics = PACKAGES[5]
        first = view(packages=[gamma_ics])
        daily = sorted((i for i in first["instances"] if i["series"] == "MAINT-DAILY"),
                       key=lambda i: i["start_utc"])
        self.assertEqual([i["start_utc"] for i in daily], [
            "2026-10-30T13:00:00+00:00", "2026-10-31T13:00:00+00:00",
            "2026-11-01T14:00:00+00:00", "2026-11-02T14:00:00+00:00"])  # 折返后 UTC 偏移 +1h
        self.assertTrue(all(i["start_local"].endswith("09:00") for i in daily))
        ambig = sorted((i for i in first["instances"] if i["series"] == "MAINT-AMBIG"),
                       key=lambda i: i["start_utc"])
        self.assertEqual([i["start_utc"] for i in ambig], [
            "2026-10-25T05:30:00+00:00", "2026-11-01T05:30:00+00:00",
            "2026-11-08T06:30:00+00:00"])  # 歧义时刻确定性取 fold=0
        again = view(packages=[gamma_ics])
        self.assertEqual([i["key"] for i in first["instances"]],
                         [i["key"] for i in again["instances"]])  # 重复采入身份稳定

    def test_conflict_graph_queries(self):
        v = view()
        graph = {"instances": v["instances"], "edges": v["edges"]}
        self.assertEqual(len(v["edges"]), 1)  # J1 与 J2 在 TC1 同窗的唯一交叠边
        edge = v["edges"][0]
        self.assertEqual(edge["crane_id"], "TC1")
        self.assertEqual((edge["start"], edge["end"]),
                         ("2026-12-01T01:00:00+00:00", "2026-12-01T02:00:00+00:00"))
        by_window = calendar_capture.query(
            graph, crane_id="TC1",
            start="2026-12-01T01:00:00+00:00", end="2026-12-01T02:00:00+00:00")
        self.assertEqual({i["job_id"] for i in by_window}, {"J1", "J2"})
        self.assertEqual(len(calendar_capture.query(graph, job_id="J2")), 1)
        self.assertEqual(len(calendar_capture.query(graph, shift="day", job_id="J2")), 1)
        j2 = calendar_capture.query(graph, job_id="J2")[0]  # 能力标注:需求 1430,TC1 可行
        self.assertEqual((j2["required_kg"], j2["rated_kg"], j2["capable"]), (1430.0, 1600, True))
        self.assertEqual(calendar_capture.query(graph, capable=False), [])
        cancelled = calendar_capture.query(graph, include_cancelled=True)
        self.assertGreater(len(cancelled), len(calendar_capture.query(graph)))

    # ------------------------------------------------------------------ 分配

    def test_greedy_first_fit_counterexample(self):
        v = view("same_shift")
        graph = {"instances": v["instances"], "edges": v["edges"]}
        greedy = calendar_capture.greedy_allocate(graph, v["jobs"], v["fleet"], tz=v["tz"])
        self.assertEqual([(r["job_id"], r["crane_id"]) for r in greedy["assignments"]],
                         [("J1", "TC1"), ("J2", "TC2")])  # 逐作业取第一台局部空闲设备
        accounts = plan_accounts()
        res = resources.evaluate([j2_asset()], accounts, allocation=greedy, fleet=FLEET)
        self.assertFalse(res["J2"]["crane_ok"])      # 额定 1300 < 1430
        self.assertEqual(res["J2"]["status"], "frozen")  # 错误的“无法同时完成”结论
        global_table = v["allocation"]
        self.assertEqual(global_table, SAME_SHIFT_TABLE)  # 全局求解:交换后两单均可行
        self.assertNotEqual(
            {r["job_id"]: r["crane_id"] for r in greedy["assignments"]},
            {r["job_id"]: r["crane_id"] for r in global_table["assignments"]})
        both = resources.evaluate([sample("asset-ok.json"), j2_asset()], accounts,
                                  allocation=global_table, fleet=FLEET)
        self.assertTrue(both["J1"]["ready"] and both["J2"]["ready"])

    def test_two_authorized_protocols_produce_distinct_tables(self):
        same = view("same_shift")["allocation"]
        preserve = view("preserve_signoff")["allocation"]
        self.assertEqual(same, SAME_SHIFT_TABLE)      # {(J1,TC2,09:00),(J2,TC1,09:00)}
        self.assertEqual(preserve, PRESERVE_TABLE)    # {(J1,TC1,09:00),(J2,待改期)}
        self.assertNotEqual(same, preserve)           # 同一组实例,授权边界不同
        self.assertTrue(all(r["job_id"] != "J2" for r in preserve["assignments"]))
        constraints = {(b["constraint"], b["crane_id"])
                       for b in preserve["pending"][0]["blocked_by"]}
        self.assertEqual(constraints, {("capacity", "TC1"), ("rating", "TC2")})

    def test_j2_projection_traces_resources_ready_into_work(self):
        accounts = plan_accounts()

        def bound(crane):
            return {"protocol": "same_shift", "pending": [],
                    "assignments": [{"job_id": "J2", "crane_id": crane,
                                     "start": "2026-12-01T09:00", "end": "2026-12-01T10:00",
                                     "from_crane": "TC1", "moved": crane != "TC1",
                                     "booking_resign": False}],
                    "objective": {"placed": 1, "moved": 0}}

        frozen = resources.evaluate([j2_asset()], accounts, allocation=bound("TC2"), fleet=FLEET)
        self.assertEqual((frozen["J2"]["rated_kg"], frozen["J2"]["crane_ok"]), (1300, False))
        self.assertEqual(frozen["J2"]["status"], "frozen")
        stopped = work.evaluate([j2_work()], accounts, frozen)
        self.assertFalse(stopped["J2"]["active"])    # source_ready 未满足
        self.assertNotEqual(stopped["J2"]["status"], "finished")
        ready = resources.evaluate([j2_asset()], accounts, allocation=bound("TC1"), fleet=FLEET)
        self.assertEqual((ready["J2"]["rated_kg"], ready["J2"]["crane_ok"]), (1600, True))
        self.assertTrue(ready["J2"]["ready"] and ready["J2"]["sling_ok"] and ready["J2"]["staff_ok"])
        done = work.evaluate([j2_work()], accounts, ready)
        self.assertEqual(done["J2"]["status"], "finished")  # 改派 TC1 后作业结果改变

    def test_cross_job_double_booking_freezes_both_without_allocation(self):
        # 没有分配表时,两个工单同时在同一塔机上声明预留 → 双方都冲突冻结
        accounts = plan_accounts()
        res = resources.evaluate([sample("asset-ok.json"), j2_asset()], accounts)
        self.assertTrue(res["J1"]["reservation_conflict"])
        self.assertTrue(res["J2"]["reservation_conflict"])
        self.assertEqual((res["J1"]["ready"], res["J2"]["ready"]), (False, False))

    # -------------------------------------------------------------- 端到端入库

    def test_store_end_to_end_same_shift_protocol(self):
        self.import_scenario("same_shift")
        rep = report(self.db)
        self.assertEqual(alloc_table(rep), {"assignments": SAME_SHIFT_TABLE["assignments"],
                                            "pending": []})
        self.assertEqual(self.entity("设备与人员资格账", "J1"), "reserved")
        self.assertEqual(self.entity("设备与人员资格账", "J2"), "reserved")
        self.assertEqual(self.entity("现场许可与作业账", "J2"), "finished")
        self.assertEqual(self.entity("日历采入与分配账", "J1"), "allocated:TC2")
        self.assertEqual(self.entity("日历采入与分配账", "J2"), "allocated:TC1")
        self.assertEqual(rep["decisions"]["J2/全局设备分配"]["result"], "TC1@2026-12-01T09:00")
        self.assertTrue(audit(self.db)["ok"])

    def test_store_end_to_end_preserve_signoff_protocol(self):
        self.import_scenario("preserve_signoff")
        rep = report(self.db)
        self.assertEqual(alloc_table(rep), {"assignments": PRESERVE_TABLE["assignments"],
                                            "pending": PRESERVE_TABLE["pending"]})
        self.assertEqual(self.entity("设备与人员资格账", "J1"), "reserved")
        self.assertEqual(self.entity("现场许可与作业账", "J1"), "finished")
        self.assertEqual(self.entity("设备与人员资格账", "J2"), "reschedule")
        self.assertEqual(self.entity("现场许可与作业账", "J2"), "reschedule")
        self.assertEqual(rep["decisions"]["J2/全局设备分配"]["result"], "待改期")
        self.assertTrue(audit(self.db)["ok"])

    def test_interrupted_recompute_and_idempotent_reimport(self):
        self.import_scenario("same_shift")
        table = alloc_table(report(self.db))
        # 重复导入幂等,分配表不变
        event = calendar_capture.calendar_event(PACKAGES[0])
        again = self.import_event(event, "calendar")
        self.assertTrue(again["idempotent"])
        self.assertEqual(alloc_table(report(self.db)), table)
        # 同 ID 异内容 → 整批回滚,分配表不变(本地编排中断不留下半成品)
        tampered = dict(event, text=event["text"] + "\n")
        with self.assertRaisesRegex(ValueError, "different content"):
            self.import_event(tampered, "calendar")
        self.assertEqual(alloc_table(report(self.db)), table)
        # 外部日历读取成功而本地编排中断:先只有日历包,无分配;补入策略后重算同一张表
        other = connect(Path(self.tmp.name) / "other.sqlite")
        try:
            for package in PACKAGES:
                ev = calendar_capture.calendar_event(package)
                path = Path(self.tmp.name) / f"o-{ev['id']}.json"
                path.write_text(json.dumps([ev], ensure_ascii=False), encoding="utf-8")
                import_file(other, "calendar", path)
            self.assertEqual(alloc_table(report(other)), {"assignments": [], "pending": []})
            for ev in [dispatch("same_shift"), sample("plan-ok.json"), j2_plan(),
                       sample("asset-ok.json"), j2_asset(), sample("work-ok.json"), j2_work()]:
                path = Path(self.tmp.name) / f"o-{ev['id']}.json"
                path.write_text(json.dumps([ev], ensure_ascii=False), encoding="utf-8")
                import_file(other, ev["kind"], path)
            self.assertEqual(alloc_table(report(other)), table)
        finally:
            other.close()
        self.assertTrue(audit(self.db)["ok"])


if __name__ == "__main__":
    unittest.main()
