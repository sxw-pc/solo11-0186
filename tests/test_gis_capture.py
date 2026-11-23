"""Offline GIS bulletin capture, spatial ruling, and cross-ledger contracts."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from liftpermit import gis_capture, plans
from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
KINDS = ('plan', 'asset', 'work')
BULLETIN = 'CITY-2026-GIS-NOTCH'
NOTCH_ROUTE = [[3, 6], [3, 3]]


def sample(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))[0]


def notch_event(**overrides):
    event = copy.deepcopy(sample('gis-notch.json'))
    event.update(overrides)
    return event


def ref(revision, bulletin=BULLETIN):
    return {"bulletin": bulletin, "revision": revision}


class CaptureUnitTests(unittest.TestCase):
    """The capturer validates, transforms and marks every segment/point."""

    def captured_zone(self, **overrides):
        return gis_capture.capture_event(notch_event(**overrides))["zones"][0]

    def test_u_notch_corridor_pending_under_public_error(self):
        zone = self.captured_zone()
        verdict, evidence = gis_capture.segment_zone_verdict([3, 6], [3, 3], zone)
        self.assertEqual(verdict, '待测')
        self.assertAlmostEqual(evidence['distance_m'], 1.0)
        self.assertEqual(evidence['error_m'], 1.5)
        self.assertEqual(evidence['bulletin'], BULLETIN)
        # 同一通道若被压成外接矩形 [1,1,5,5] 会误判为相交
        self.assertTrue(plans._segment_hits_box([3, 6], [3, 3], [1, 1, 5, 5]))

    def test_u_notch_corridor_clear_under_signed_survey(self):
        zone = dict(self.captured_zone(), error_m=0.2, error_source='signed_survey')
        verdict, evidence = gis_capture.segment_zone_verdict([3, 6], [3, 3], zone)
        self.assertEqual(verdict, '可通行')
        self.assertLess(evidence['error_m'], evidence['distance_m'])

    def test_segment_through_u_arm_is_forbidden(self):
        verdict, _ = gis_capture.segment_zone_verdict([0, 1.5], [6, 1.5], self.captured_zone())
        self.assertEqual(verdict, '禁止')

    def test_grazing_segment_depends_on_boundary_convention(self):
        closed = self.captured_zone()
        verdict, _ = gis_capture.segment_zone_verdict([5, 6], [5, 3], closed)
        self.assertEqual(verdict, '禁止')  # 闭合边界本身属于禁区
        open_exact = dict(closed, boundary='open', error_m=0.0)
        verdict, _ = gis_capture.segment_zone_verdict([5, 6], [5, 3], open_exact)
        self.assertEqual(verdict, '可通行')  # 开放边界且零误差，擦边不算穿越
        open_uncertain = dict(closed, boundary='open', error_m=0.2)
        verdict, _ = gis_capture.segment_zone_verdict([5, 6], [5, 3], open_uncertain)
        self.assertEqual(verdict, '待测')  # 有测量误差时擦边不可直接放行

    def test_hole_ring_is_not_forbidden(self):
        event = notch_event(id='GIS-HOLE', control_error_m=0.0)
        event['geojson'] = {"type": "Polygon", "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}
        zone = gis_capture.capture_event(event)['zones'][0]
        verdict, _ = gis_capture.segment_zone_verdict([5, 4.5], [5, 5.5], zone)
        self.assertEqual(verdict, '可通行')  # 孔洞内部不属于禁区
        verdict, _ = gis_capture.segment_zone_verdict([5, 0], [5, 10], zone)
        self.assertEqual(verdict, '禁止')  # 穿越环体
        verdict, _ = gis_capture.point_zone_verdict([5, 5], zone)
        self.assertEqual(verdict, '可通行')
        verdict, _ = gis_capture.point_zone_verdict([5, 3], zone)
        self.assertEqual(verdict, '禁止')

    def test_cross_crs_bulletin_transforms_to_site_plane(self):
        event = notch_event(id='GIS-CRS',
                            crs={"name": "city-grid", "origin": [100, 50], "scale": 0.5})
        event['geojson'] = {"type": "Polygon", "coordinates": [[
            [102, 52], [110, 52], [110, 60], [108, 60],
            [108, 54], [104, 54], [104, 60], [102, 60], [102, 52]]]}
        zone = gis_capture.capture_event(event)['zones'][0]
        self.assertEqual(zone['rings'][0],
                         [[1, 1], [5, 1], [5, 5], [4, 5], [4, 2], [2, 2], [2, 5], [1, 5], [1, 1]])
        verdict, _ = gis_capture.segment_zone_verdict([3, 6], [3, 3], zone)
        self.assertEqual(verdict, '待测')  # 跨 CRS 变换后空间裁定一致

    def test_invalid_bulletins_are_rejected_not_emptied(self):
        bad = notch_event()
        bad['geojson']['features'][0]['geometry']['coordinates'][0].pop(0)
        with self.assertRaises(ValueError):
            gis_capture.capture_event(bad)  # 环不闭合
        with self.assertRaises(ValueError):
            gis_capture.capture_event(notch_event(crs={"name": "mars-local"}))  # 未知 CRS
        with self.assertRaises(ValueError):
            gis_capture.capture_event(notch_event(
                geojson={"type": "FeatureCollection", "features": []}))  # 空公告
        with self.assertRaises(ValueError):
            gis_capture.capture_event(notch_event(control_error_m=None))  # 无误差上界
        bad = notch_event()
        del bad['control_error_m']
        with self.assertRaises(ValueError):
            gis_capture.capture_event(bad)

    def test_forward_revocation_caps_zone_window(self):
        zone = self.captured_zone(revoked_at='2026-12-01T08:30')
        self.assertEqual(zone['end'], '2026-12-01T08:30')
        self.assertEqual(zone['start'], '2026-12-01T08:00')

    def test_retroactive_retraction_voids_earlier_revisions(self):
        rev1 = gis_capture.capture_event(notch_event())
        retraction = notch_event(id='GIS2', revision=2, retraction='retroactive',
                                 revoked_at='2026-12-01T09:45')
        retraction.pop('geojson')
        rev2 = gis_capture.capture_event(retraction)
        self.assertEqual(rev2['zones'], [])
        plan = {"kind": "plan", "zones": [ref(1)]}
        gis_capture.resolve_events([rev1, rev2, plan])
        self.assertTrue(plan['zones'])
        self.assertTrue(all(z.get('void') for z in plan['zones']))  # 证据保留但不再生效

    def test_missing_bulletin_reference_fails_closed(self):
        plan = {"kind": "plan", "zones": [ref(1, bulletin='NEVER-IMPORTED')]}
        gis_capture.resolve_events([plan])
        clear, evidence = plans._route_ruling(NOTCH_ROUTE, plan['zones'],
                                              '2026-12-01T09:00', '2026-12-01T10:00')
        self.assertFalse(clear)
        self.assertEqual(evidence[0]['verdict'], '禁止')

    def test_plan_route_state_tracks_survey_precision(self):
        zone = self.captured_zone()
        plan = {"id": "PX", "kind": "plan", "job_id": "JX", "version": 1,
                "window_start": "2026-12-01T09:00", "window_end": "2026-12-01T10:00",
                "radius_m": 8, "boom_m": 20,
                "measurements": [{"id": "M1", "type": "independent", "kg": 100,
                                  "at": "2026-12-01T08:00", "witness": "w"}],
                "center": [0, 0], "hook_points": [[-1, -1], [1, 1]],
                "route": NOTCH_ROUTE, "zones": [zone], "signed_version": 1}
        account = plans.evaluate([plan])["JX"]
        self.assertEqual(account['route_state'], '待测')
        self.assertFalse(account['ready'])  # 不确定线路不得直接放行
        precise = plans.evaluate([dict(plan, zones=[dict(zone, error_m=0.2)])])["JX"]
        self.assertEqual(precise['route_state'], '可通行')
        self.assertTrue(precise['ready'])


class GisWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'gis.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, kind, name):
        return import_file(self.db, kind, ROOT / 'fixtures' / name)

    def import_event(self, event):
        path = Path(self.tmp.name) / f"{event['id']}.json"
        path.write_text(json.dumps([event], ensure_ascii=False), encoding='utf-8')
        return import_file(self.db, event['kind'], path)

    def normal(self):
        for kind in KINDS:
            self.load(kind, f'{kind}-ok.json')

    def decision(self, name):
        return report(self.db)['decisions'][f'J1/{name}']['result']

    def plan_v(self, version, zones, route=None):
        plan = copy.deepcopy(sample('plan-ok.json'))
        plan.update(id=f'P{version}', version=version, signed_version=version, zones=zones)
        if route is not None:
            plan['route'] = route
        return plan

    def survey_revision(self):
        event = notch_event(id='GIS2', revision=2)
        event['survey'] = {"signed_by": "surveyor-7", "at": "2026-12-01T07:30",
                           "error_m": 0.2, "center_remeasured": True,
                           "control_points": [{"id": "CP1", "xy": [0, 0]},
                                              {"id": "CP2", "xy": [12, 8]}]}
        return event

    def test_conservative_survey_flow_redraws_and_stops(self):
        self.normal()
        self.load('gis', 'gis-notch.json')
        self.import_event(self.plan_v(2, [ref(1)], route=NOTCH_ROUTE))
        # 公开误差上界 1.5m 大于通道净距 1.0m：路线进入不确定带，裁定重画
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        self.assertEqual(self.decision('现场风速停复工'), '停工')  # J1 原窗停工
        self.assertTrue(audit(self.db)['ok'])

    def test_precision_survey_flow_clears_and_finishes(self):
        self.normal()
        self.load('gis', 'gis-notch.json')
        self.import_event(self.plan_v(2, [ref(1)], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        self.import_event(self.survey_revision())
        # 新公告版本不会自动改写已签方案的裁定
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        self.import_event(self.plan_v(3, [ref(2)], route=NOTCH_ROUTE))
        # 签章控制点把总误差压到 0.2m，通道净距 1.0m 可证明大于总误差
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')
        self.assertEqual(self.decision('指挥资质排班确认'), '覆盖')  # 设备与人员仍覆盖窗口
        work = copy.deepcopy(sample('work-ok.json'))
        work.update(id='W2', version=2)
        work['permit_actions'] = [{'id': 'K2', 'at': '2026-12-01T08:55',
                                   'action': 'release', 'plan_version': 3}]
        self.import_event(work)
        self.assertEqual(self.decision('现场风速停复工'), '放行')
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        self.assertTrue(audit(self.db)['ok'])

    def test_mid_window_bulletin_and_correction(self):
        self.normal()
        mid = notch_event(id='GIS-MID', bulletin_id='CITY-2026-GIS-MID',
                          effective_from='2026-12-01T09:30',
                          effective_until='2026-12-01T11:00')
        self.import_event(mid)
        self.import_event(self.plan_v(2, [ref(1, 'CITY-2026-GIS-MID')], route=NOTCH_ROUTE))
        # 公告在作业窗中途生效，覆盖窗口的部分仍须核对
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        correction = notch_event(id='GIS-MID2', bulletin_id='CITY-2026-GIS-MID', revision=2,
                                 effective_from='2026-12-01T09:30',
                                 effective_until='2026-12-01T11:00')
        correction['geojson']['features'][0]['geometry']['coordinates'][0] = [
            [7, 1], [11, 1], [11, 5], [10, 5], [10, 2], [8, 2], [8, 5], [7, 5], [7, 1]]
        self.import_event(correction)
        self.import_event(self.plan_v(3, [ref(2, 'CITY-2026-GIS-MID')], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')  # 更正后禁区让开通道
        self.assertTrue(audit(self.db)['ok'])

    def test_retroactive_retraction_reopens_route(self):
        self.normal()
        self.load('gis', 'gis-notch.json')
        self.import_event(self.plan_v(2, [ref(1)], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        retraction = notch_event(id='GIS2', revision=2, retraction='retroactive',
                                 revoked_at='2026-12-01T09:45')
        retraction.pop('geojson')
        self.import_event(retraction)
        self.import_event(self.plan_v(3, [ref(2)], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')  # 禁区自始不生效
        records = report(self.db)['business_records']
        self.assertTrue(any(r['entity_type'] == '禁入区公告' for r in records))  # 证据保留
        self.assertTrue(audit(self.db)['ok'])

    def test_new_bulletin_blocks_original_landing_site(self):
        self.normal()
        bulletin = notch_event(id='GIS-LS', bulletin_id='CITY-2026-GIS-LS',
                               control_error_m=0.2)
        bulletin['geojson'] = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "ls1-cover"},
             "geometry": {"type": "Polygon", "coordinates": [
                 [[-2, -4], [2, -4], [2, -1], [-2, -1], [-2, -4]]]}}]}
        self.import_event(bulletin)
        self.import_event(self.plan_v(2, [ref(1, 'CITY-2026-GIS-LS')]))
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')
        lifted = copy.deepcopy(sample('work-ok.json'))
        lifted.update(id='W2', version=2)
        lifted['hook_events'] = lifted['hook_events'][:2]
        self.import_event(lifted)
        asset = copy.deepcopy(sample('asset-ok.json'))
        asset.update(id='A2', version=2)
        asset['crane']['faults'] = [{'at': '2026-12-01T09:16', 'reason': 'brake'}]
        self.import_event(asset)
        # LS1 落入新禁区，现场账按同一公告版本改选 LS2
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS2')
        wider = notch_event(id='GIS-LS2', bulletin_id='CITY-2026-GIS-LS', revision=2,
                            control_error_m=0.2)
        wider['geojson'] = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "ls1-cover"},
             "geometry": {"type": "Polygon", "coordinates": [
                 [[-2, -4], [2, -4], [2, -1], [-2, -1], [-2, -4]]]}},
            {"type": "Feature", "properties": {"name": "ls2-cover"},
             "geometry": {"type": "Polygon", "coordinates": [
                 [[4, 4], [7, 4], [7, 7], [4, 7], [4, 4]]]}}]}
        self.import_event(wider)
        self.import_event(self.plan_v(3, [ref(2, 'CITY-2026-GIS-LS')]))
        self.assertEqual(self.decision('悬吊物应急落地'), '无安全落点')
        self.assertTrue(audit(self.db)['ok'])

    def test_legacy_rect_replay_and_no_silent_bbox(self):
        self.normal()
        self.load('gis', 'gis-notch.json')
        # 旧记录只存矩形：公告接入不改变其原语义回放
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')
        legacy = [{"rect": [5, 5, 6, 6], "start": "2026-12-01T08:00",
                   "end": "2026-12-01T11:00"}]
        self.import_event(self.plan_v(2, legacy, route=[[0, 0], [7, 7]]))
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')  # 矩形真穿越
        bbox = [{"rect": [1, 1, 5, 5], "start": "2026-12-01T08:00",
                 "end": "2026-12-01T11:00"}]
        self.import_event(self.plan_v(3, bbox, route=NOTCH_ROUTE))
        # 外接矩形近似会把 U 形通道误判为重画，多边形裁定不得被它悄悄替换
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')

    def test_bad_gis_file_never_opens_route(self):
        self.normal()
        bad = notch_event(crs={"name": "unknown-grid"})
        path = Path(self.tmp.name) / 'bad-gis.json'
        path.write_text(json.dumps([bad], ensure_ascii=False), encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.db, 'gis', path)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0], 3)
        # 公告未落库，引用它的方案失败关闭，而不是按空禁区放行
        self.import_event(self.plan_v(2, [ref(1)], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_old_hooks_not_ratified_by_new_map(self):
        self.normal()
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        self.load('gis', 'gis-notch.json')
        self.import_event(self.survey_revision())
        self.import_event(self.plan_v(3, [ref(2)], route=NOTCH_ROUTE))
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')
        self.assertEqual(self.decision('方案签认撤回'), '有效')
        # 新地图与新方案不产生新许可：旧 release 与旧钩序不被自动追认
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')


if __name__ == '__main__':
    unittest.main()
