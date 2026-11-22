import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import liftpermit
from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
KINDS = {'plan': 'plan', 'asset': 'asset', 'work': 'work'}


def sample(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))[0]


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

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
        return report(self.db)['decisions']

    def decision(self, name):
        return report(self.db)['decisions'][f'J1/{name}']['result']

    def test_module_path_and_twelve_independent_results(self):
        results = self.normal()
        print('imported module:', liftpermit.__file__)
        self.assertTrue(Path(liftpermit.__file__).resolve().is_relative_to(ROOT))
        self.assertEqual(len(results), 12)
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        self.assertTrue(audit(self.db)['ok'])

    def test_three_subsystems_persist_distinct_entities(self):
        self.normal()
        records = report(self.db)['business_records']
        by_subsystem = {}
        for row in records:
            by_subsystem.setdefault(row['subsystem'], set()).add(row['entity_type'])
        self.assertEqual(len(by_subsystem), 3)
        self.assertTrue(all(len(names) >= 2 for names in by_subsystem.values()))
        self.assertGreaterEqual(sum(map(len, by_subsystem.values())), 9)

    def test_independent_weight_measurement_overrides_estimate(self):
        self.normal()
        self.assertEqual(self.decision('吊物重量版本核定'), '有效')
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered['id'], altered['version'], altered['signed_version'] = 'P2', 2, 2
        altered['measurements'][-1]['kg'] = 1600
        altered['measurements'][-1]['id'] = 'M2'
        self.import_event(altered)
        self.assertEqual(self.decision('塔机额定载荷复核'), '超限或检验失效')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_center_outside_hook_envelope_revokes_plan_and_work(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2, center=[3, 0])
        self.import_event(altered)
        self.assertEqual(self.decision('吊点重心方案签认'), '撤回')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_center_in_bounding_box_but_off_two_point_support_line(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2, center=[0, 0.7])
        self.import_event(altered)
        self.assertEqual(self.decision('吊点重心方案签认'), '撤回')

    def test_segment_crossing_active_zone_requires_redraw(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2,
                       route=[[0, 0], [7, 7]])
        self.import_event(altered)
        self.assertEqual(self.decision('回转路径禁区核验'), '重画')

    def test_zone_outside_work_window_does_not_block_path(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2, route=[[0, 0], [7, 7]])
        altered['zones'][0]['start'] = '2026-12-01T11:00'
        altered['zones'][0]['end'] = '2026-12-01T12:00'
        self.import_event(altered)
        self.assertEqual(self.decision('回转路径禁区核验'), '可通行')

    def test_old_signature_does_not_follow_new_plan(self):
        self.normal()
        self.load('plan', 'plan-bad.json')
        self.assertEqual(self.decision('方案签认撤回'), '撤回')

    def test_explicit_plan_revocation_stops_authorized_work(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2, revoked=True)
        self.import_event(altered)
        self.assertEqual(self.decision('方案签认撤回'), '撤回')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_same_version_plan_conflict_freezes_work(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered['id'] = 'P1-conflict'
        altered['radius_m'] = 9
        self.import_event(altered)
        self.assertEqual(self.decision('吊物重量版本核定'), '待复测')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_rating_curve_uses_radius_and_boom_not_single_flag(self):
        self.normal()
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2, radius_m=11)
        self.import_event(altered)
        self.assertEqual(self.decision('塔机额定载荷复核'), '超限或检验失效')

    def test_sling_angle_derating_and_scrap(self):
        self.normal()
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['slings'][0]['angle_deg'] = 70
        self.import_event(altered)
        self.assertEqual(self.decision('索具组合适配'), '失配')
        altered['id'], altered['version'] = 'A3', 3
        altered['slings'][0].update(angle_deg=30, status='scrapped')
        self.import_event(altered)
        self.assertEqual(self.decision('索具组合适配'), '失配')

    def test_commander_shift_gap_revokes_permit(self):
        self.normal()
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['commander']['shifts'][0]['end'] = '2026-12-01T09:15'
        self.import_event(altered)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_expired_commander_certificate_blocks_coverage(self):
        self.normal()
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['commander']['cert_until'] = '2026-11-30T23:59'
        self.import_event(altered)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')

    def test_overlapping_crane_reservation_conflicts(self):
        self.normal()
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['competing_reservations'] = [
            {'job_id': 'J2', 'crane_id': 'TC1', 'start': '2026-12-01T09:20',
             'end': '2026-12-01T10:20'}]
        self.import_event(altered)
        self.assertEqual(self.decision('设备保留冲突'), '冲突')

    def test_released_crane_reservation_revokes_work_permit(self):
        self.normal()
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2, reservation_status='released')
        self.import_event(altered)
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        state = self.db.execute("SELECT state FROM entity_state WHERE subsystem='设备与人员资格账'").fetchone()[0]
        self.assertEqual(state, 'released')

    def test_two_high_calibrated_winds_stop_lift(self):
        self.normal()
        self.load('work', 'work-bad.json')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS1')

    def test_single_prestart_wind_source_cannot_authorize_release(self):
        self.normal()
        altered = copy.deepcopy(sample('work-ok.json'))
        altered.update(id='W2', version=2)
        altered['wind_samples'] = altered['wind_samples'][:1]
        self.import_event(altered)
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_uncalibrated_clear_reading_cannot_resume(self):
        self.normal()
        altered = copy.deepcopy(sample('work-recover.json'))
        altered['id'], altered['version'] = 'W2', 2
        altered['wind_samples'][-2]['calibrated_until'] = '2026-11-30T00:00'
        altered['wind_samples'][-1]['calibrated_until'] = '2026-11-30T00:00'
        altered['permit_actions'][-1]['plan_version'] = 1
        self.import_event(altered)
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_out_of_order_hook_sequence_does_not_finish(self):
        self.normal()
        altered = copy.deepcopy(sample('work-ok.json'))
        altered.update(id='W2', version=2)
        altered['hook_events'][1]['at'] = '2026-12-01T08:40'
        self.import_event(altered)
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')

    def test_commander_cannot_sign_as_independent_monitor(self):
        self.normal()
        altered = copy.deepcopy(sample('work-ok.json'))
        altered.update(id='W2', version=2)
        altered['hook_events'][-1]['witness'] = 'Q1'
        self.import_event(altered)
        self.assertEqual(self.decision('监理独立见证'), '待见证')
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')

    def test_fault_while_lifted_creates_landing_duty(self):
        self.normal()
        lifted = copy.deepcopy(sample('work-ok.json'))
        lifted.update(id='W2', version=2)
        lifted['hook_events'] = lifted['hook_events'][:2]
        self.import_event(lifted)
        self.assertEqual(self.decision('悬吊物应急落地'), '无需')
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['crane']['faults'] = [{'at': '2026-12-01T09:16', 'reason': 'brake'}]
        self.import_event(altered)
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS1')

    def test_scrapped_sling_while_lifted_creates_landing_duty(self):
        self.normal()
        lifted = copy.deepcopy(sample('work-ok.json'))
        lifted.update(id='W2', version=2)
        lifted['hook_events'] = lifted['hook_events'][:2]
        self.import_event(lifted)
        self.assertEqual(self.decision('悬吊物应急落地'), '无需')
        altered = copy.deepcopy(sample('asset-ok.json'))
        altered.update(id='A2', version=2)
        altered['slings'][0]['status'] = 'scrapped'
        self.import_event(altered)
        self.assertEqual(self.decision('索具组合适配'), '失配')
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS1')

    def test_no_capacity_or_safe_geometry_blocks_emergency_landing(self):
        self.normal()
        altered = copy.deepcopy(sample('work-bad.json'))
        altered.update(id='W2-no-site', version=2)
        altered['landing_sites'][0]['capacity_kg'] = 500
        self.import_event(altered)
        self.assertEqual(self.decision('悬吊物应急落地'), '无安全落点')

    def test_identical_file_replay_and_collision_rollback(self):
        first = self.load('plan', 'plan-ok.json')
        again = self.load('plan', 'plan-ok.json')
        self.assertTrue(again['idempotent'])
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered['radius_m'] = 99
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.import_event(altered)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)

    def test_audit_detects_materialized_tampering(self):
        self.normal()
        self.db.execute("UPDATE decisions SET result='FORGED' WHERE name='J1/现场风速停复工'")
        self.db.commit()
        self.assertFalse(audit(self.db)['ok'])

    def test_cli_demo_on_fresh_database(self):
        db = Path(self.tmp.name) / 'cli.sqlite'
        proc = subprocess.run([sys.executable, '-m', 'liftpermit', 'demo', '--db', str(db)],
                              cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data['imports']), 9)
        self.assertTrue(data['audit']['ok'])
        self.assertEqual(data['imports'][-1]['work_state'], 'finished')


if __name__ == '__main__':
    unittest.main()
