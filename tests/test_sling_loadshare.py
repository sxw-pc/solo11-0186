"""Three-point sling load share: measured, equal-division, and geometry sources.

Synthetic J7: hooks (-1,0), (1,0), (0,1); independent remeasurement 1800 kg at
center (0.8,0.1).  The center is inside the triangle, so the old
``plans._center_supported`` and the old equal-division ``resources`` threshold
both pass (1800x1.1/3 = 660 kg < 866 kg derated leg).  The actual rigid-static
static split is (0.05, 0.85, 0.10): the sling nearest hook 2 carries 1683 kg of
vertical dynamic load, well over 866 kg.  Nothing in this file may collapse
that back to "total tension is fine".
"""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from liftpermit import plans, resources, tension_capture, rules
from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]

HOOK_POINTS = [(-1.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
CENTER = (0.8, 0.1)
MASS_KG = 1800.0
CAPACITY_KG = 1000.0
ANGLE_DEG = 30.0
DERATED_KG = CAPACITY_KG * math.cos(math.radians(ANGLE_DEG))
GAIN = 0.1019  # kgf per Newton, from the meter certificate

EXPECTED_SHARES = (0.05, 0.85, 0.10)
EXPECTED_ENVELOPE = (99.0, 1683.0, 198.0)  # static x 1.1


def fixture(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))[0]


def fixture_list(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))


class GeometryModelTests(unittest.TestCase):
    def test_center_inside_triangle_but_static_split_is_uneven(self):
        self.assertTrue(plans._center_supported(list(CENTER), list(HOOK_POINTS)))
        shares = plans._triangle_shares(list(CENTER), [list(p) for p in HOOK_POINTS])
        for got, want in zip(shares, EXPECTED_SHARES):
            self.assertAlmostEqual(got, want, places=12)

    def test_old_equal_division_passes_where_geometry_fails(self):
        plan = plans.evaluate(fixture_list('plan-j7.json'))['J7']
        asset = fixture('asset-j7.json')
        account = resources.evaluate([asset], {'J7': plan})['J7']
        sources = account['sling_sources']
        # The old textbook check (mass x 1.1 / count) still says yes...
        self.assertTrue(sources['equal_division']['ok'])
        self.assertAlmostEqual(sources['equal_division']['per_sling_kg'], 660.0, places=6)
        # ...but the per-slng envelope says no, and that governs.
        self.assertFalse(account['sling_ok'])
        walls = [w['envelope_kg'] for w in sources['geometry']['walls']]
        for got, want in zip(walls, EXPECTED_ENVELOPE):
            self.assertAlmostEqual(got, want, places=6)
        self.assertAlmostEqual(sources['geometry']['worst']['envelope_kg'], 1683.0, places=6)
        self.assertGreater(sources['geometry']['worst']['envelope_kg'], DERATED_KG)

    def test_two_point_plan_keeps_old_equal_division_meaning(self):
        # fixtures/plan-ok.json is a two-hook line; geometry cannot split it,
        # so the original equal-division verdict must survive unchanged.
        plan = plans.evaluate(fixture_list('plan-ok.json'))['J1']
        self.assertIsNone(plan['envelope'])
        account = resources.evaluate([fixture('asset-ok.json')], {'J1': plan})['J1']
        self.assertTrue(account['sling_ok'])
        self.assertIsNone(account['sling_sources']['geometry'])
        self.assertTrue(account['sling_sources']['equal_division']['ok'])


class CaptureBatchTests(unittest.TestCase):
    def setUp(self):
        self.plan = plans.evaluate(fixture_list('plan-j7.json'))['J7']
        self.asset = resources.evaluate([fixture('asset-j7.json')], {'J7': self.plan})['J7']
        self.batch = fixture('sling-imbalance.json')

    def reseal(self, batch):
        return tension_capture.seal(batch)

    def test_fixture_converts_and_detects_second_sling_overload(self):
        result = tension_capture.loadshare(self.batch, self.plan, self.asset)
        self.assertEqual(result['status'], 'overload')
        self.assertEqual(result['verified'], True)
        measured = [row['measured_kgf'] for row in result['rows']]
        for got, want in zip(measured, (90.0, 1530.0, 180.0)):
            self.assertAlmostEqual(got, want, places=1)
        self.assertAlmostEqual(result['measured_total_kgf'], MASS_KG, places=1)
        second = result['rows'][1]
        self.assertEqual(second['sling_id'], 'S2')
        self.assertGreater(second['measured_kgf'], second['derated_kg'])
        self.assertAlmostEqual(second['measured_utilization'],
                               1530.0 / DERATED_KG, places=6)
        # Measured evidence fails => the resource ledger must fail.
        account = resources.evaluate([fixture('asset-j7.json')], {'J7': self.plan},
                                     {'J7': result})['J7']
        self.assertFalse(account['sling_ok'])
        self.assertTrue(account['sling_sources']['measured']['applies_to_current_plan'])
        self.assertFalse(account['sling_sources']['measured']['accepted'])

    def test_average_or_total_only_would_hide_overload(self):
        # This is the exact regression the task warns about: three verified
        # readings whose mean (600 kg) and total (1800 kg) both look safe.
        result = tension_capture.loadshare(self.batch, self.plan, self.asset)
        mean_kgf = sum(r['measured_kgf'] for r in result['rows']) / 3
        self.assertLess(mean_kgf, DERATED_KG)
        self.assertAlmostEqual(result['measured_total_kgf'], MASS_KG, places=1)
        self.assertFalse(result['measured_ok'])
        self.assertEqual(result['status'], 'overload')

    def test_pre_lift_and_post_lift_peaks_are_excluded(self):
        result = tension_capture.loadshare(self.batch, self.plan, self.asset)
        # The pre-lift test pull peaked at 1600 kgf on every sling; it must not
        # leak into the off-ground utilisation.
        self.assertAlmostEqual(result['excluded_peaks']['pre_lift_peak'], 1600.0, places=1)
        self.assertAlmostEqual(result['excluded_peaks']['post_lift_peak'], 1200.0, places=1)
        for row in result['rows']:
            self.assertLess(row['measured_kgf'], 1600.0)
        self.assertAlmostEqual(result['rows'][1]['measured_kgf'], 1530.0, places=1)

    def test_missing_reading_is_unpaired_not_backfilled(self):
        broken = copy.deepcopy(self.batch)
        broken['events'] = [e for e in broken['events'] if e['id'] != 'R3-off']
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('unpaired', result['flags'])
        third = result['rows'][2]
        self.assertEqual(third['status'], 'unpaired')
        self.assertIsNone(third['measured_kgf'])
        # Crucially the surviving rows are not inflated to mass / 2 either.
        self.assertAlmostEqual(result['rows'][1]['measured_kgf'], 1530.0, places=1)
        self.assertFalse(result['measured_ok'])

    def test_one_rfid_claimed_by_two_meters_is_misbound(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['id'] == 'R3-off':
                event['sling_rfid'] = 'RFID-S2'
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('misbound', result['flags'])
        self.assertFalse(result['measured_ok'])

    def test_hook_point_sling_swap_is_misbound(self):
        # Meter says HP1 but the plan binds RFID-S1 to HP1; swapping the two
        # off-ground hook ids must be caught against the signed geometry.
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['id'] == 'R1-off':
                event['hook_id'] = 'HP2'
            elif event['id'] == 'R2-off':
                event['hook_id'] = 'HP1'
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('misbound', result['flags'])
        self.assertFalse(result['measured_ok'])

    def test_over_range_reading_is_flagged(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['id'] == 'R2-off':
                event['range_n'] = 12000.0
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('over_range', result['flags'])
        self.assertEqual(result['rows'][1]['status'], 'over_range')

    def test_clock_drift_of_one_meter_is_not_synchronised(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['id'] == 'R2-off':
                event['at'] = '2026-12-01T09:15'
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('uncalibrated', result['flags'])
        self.assertEqual(result['rows'][1]['status'], 'uncalibrated')
        self.assertFalse(result['measured_ok'])

    def test_expired_calibration_cannot_prove_overload(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            event['calibration'] = dict(event['calibration'],
                                        valid_until='2026-11-30T00:00')
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('uncalibrated', result['flags'])
        self.assertFalse(result['measured_ok'])

    def test_zero_reset_and_changed_curve_break_the_batch(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['meter_uid'] == 'TM-2':
                break
        for event in broken['events']:
            if event['meter_uid'] == 'TM-2' and event['stage'] == 'post_lift':
                event['zero_reset_at'] = '2026-12-01T09:14'
                event['calibration'] = dict(event['calibration'], kgf_per_n=0.2)
        broken = self.reseal(broken)
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertIn('uncalibrated', result['flags'])
        self.assertFalse(result['measured_ok'])

    def test_tampered_digest_is_unverified(self):
        broken = copy.deepcopy(self.batch)
        for event in broken['events']:
            if event['id'] == 'R2-off':
                event['value_n'] = 9000.0
        # Deliberately do NOT reseal: the exported digest no longer matches.
        result = tension_capture.loadshare(broken, self.plan, self.asset)
        self.assertFalse(result['verified'])
        self.assertEqual(result['status'], 'unverified')
        self.assertFalse(result['measured_ok'])


class ScopeAndVersionTests(unittest.TestCase):
    def setUp(self):
        self.plan = plans.evaluate(fixture_list('plan-j7.json'))['J7']
        self.batch = fixture('sling-imbalance.json')

    def test_old_capture_does_not_apply_to_new_center(self):
        moved = copy.deepcopy(fixture('plan-j7.json'))
        moved.update(id='P7-moved', version=2, signed_version=2, center=[0.0, 0.0])
        new_plan = plans.evaluate([moved])['J7']
        self.assertNotEqual(new_plan['geometry_sha'], self.plan['geometry_sha'])
        # The old plan still exposes the old measurement...
        old_asset = resources.evaluate([fixture('asset-j7.json')], {'J7': self.plan})['J7']
        capture = tension_capture.loadshare(self.batch, self.plan, old_asset)
        # ...but it is stale against the new geometry, not a fresh clearance.
        stale = tension_capture.loadshare(self.batch, new_plan, old_asset)
        self.assertIn('stale', stale['flags'])
        account = resources.evaluate([fixture('asset-j7.json')], {'J7': new_plan},
                                     {'J7': stale})['J7']
        self.assertFalse(account['sling_sources']['measured']['applies_to_current_plan'])
        self.assertFalse(account['sling_ok'])
        # The old reading keeps its old verdict rather than being rewritten.
        self.assertEqual(capture['status'], 'overload')

    def test_new_hook_version_invalidates_capture_even_with_same_center(self):
        altered = copy.deepcopy(fixture('plan-j7.json'))
        altered['hook_points'][2]['point'] = [0.0, 1.5]
        altered.update(id='P7-hook', version=2, signed_version=2)
        new_plan = plans.evaluate([altered])['J7']
        stale = tension_capture.loadshare(self.batch, new_plan,
                                          resources.evaluate([fixture('asset-j7.json')],
                                                             {'J7': new_plan})['J7'])
        self.assertIn('stale', stale['flags'])
        self.assertFalse(stale['measured_ok'])

    def test_unrelated_version_bump_with_same_geometry_still_applies(self):
        bumped = copy.deepcopy(fixture('plan-j7.json'))
        bumped.update(id='P7-same-geom', version=2, signed_version=2)
        new_plan = plans.evaluate([bumped])['J7']
        self.assertEqual(new_plan['geometry_sha'], self.plan['geometry_sha'])
        account = resources.evaluate([fixture('asset-j7.json')], {'J7': new_plan})['J7']
        result = tension_capture.loadshare(self.batch, new_plan, account)
        # A re-signed plan with the same hook geometry is still the same lift:
        # the measurement is not discarded just because the version integer
        # moved, and its overload verdict still applies.
        self.assertNotIn('stale', result['flags'])
        self.assertEqual(result['current_plan_version'], 2)
        self.assertEqual(result['status'], 'overload')


class ConservativeRouteTests(unittest.TestCase):
    """Route one: signed hook coordinates + rigid-static bound, no third meter."""

    def setUp(self):
        self.plan = plans.evaluate(fixture_list('plan-j7.json'))['J7']

    def test_static_bound_alone_blocks_the_lift(self):
        # Without any meter evidence the geometry envelope is the *best*
        # available proof and it already exceeds sling 2's derated capacity.
        asset = fixture('asset-j7.json')
        account = resources.evaluate([asset], {'J7': self.plan})['J7']
        self.assertFalse(account['sling_ok'])
        worst = account['sling_sources']['geometry']['worst']
        self.assertEqual(worst['index'], 1)
        self.assertGreaterEqual(worst['envelope_kg'], 1683.0 - 1e-6)
        self.assertLess(worst['static_kg'], worst['envelope_kg'])

    def test_conservative_bound_is_a_policy_stop_not_a_measurement(self):
        # The bound must remain labelled as analysis; it must never be
        # reported as a measured tension to back-fill the missing meter.
        account = resources.evaluate([fixture('asset-j7.json')],
                                     {'J7': self.plan})['J7']
        self.assertIsNone(account['sling_sources']['measured'])
        self.assertEqual(account['sling_sources']['geometry']['source'], 'analysis')
        self.assertEqual(account['sling_sources']['geometry']['model'],
                         'rigid_static_barycentric')

    def test_unsupported_geometry_does_not_invent_a_bound(self):
        event = copy.deepcopy(fixture('plan-j7.json'))
        event['center'] = [3.0, 3.0]  # well outside the triangle
        plan = plans.evaluate([event])['J7']
        self.assertFalse(plan['center_ok'])
        self.assertIsNone(plan['envelope'])
        self.assertIsNone(plan['load_share']['shares'])
        account = resources.evaluate([fixture('asset-j7.json')], {'J7': plan})['J7']
        self.assertIsNone(account['sling_sources']['geometry'])


class BeamRetrofitTests(unittest.TestCase):
    def setUp(self):
        self.plan = plans.evaluate(fixture_list('plan-j7.json'))['J7']

    def test_static_only_beam_certificate_remains_pending(self):
        asset = fixture('asset-j7-beam-static.json')
        account = resources.evaluate([asset], {'J7': self.plan})['J7']
        beam = account['sling_sources']['equalizing_beam']
        self.assertEqual(beam['status'], 'pending_off_ground_certificate')
        self.assertFalse(beam['covers_off_ground'])
        self.assertFalse(account['sling_ok'])

    def test_off_ground_beam_proves_every_leg_under_800kg(self):
        asset = fixture('asset-j7-beam.json')
        account = resources.evaluate([asset], {'J7': self.plan})['J7']
        beam = account['sling_sources']['equalizing_beam']
        self.assertTrue(beam['ok'])
        self.assertTrue(account['sling_ok'])
        for leg in beam['legs']:
            self.assertAlmostEqual(leg['peak_kg'], 660.0, places=6)  # 1800*1.1/3
            self.assertLessEqual(leg['peak_kg'], 800.0)

    def test_beam_does_not_backdate_the_old_off_ground_failure(self):
        # Old capture retains its overload verdict; the beam only clears the
        # *new* asset configuration.
        batch = fixture('sling-imbalance.json')
        base_asset = fixture('asset-j7.json')
        base_account = resources.evaluate([base_asset], {'J7': self.plan})['J7']
        old = tension_capture.loadshare(batch, self.plan, base_account)
        self.assertEqual(old['status'], 'overload')
        # Feed the old capture into the retrofitted asset: it must not turn the
        # historic reading into a clean one.
        retrofit_account = resources.evaluate([fixture('asset-j7-beam.json')],
                                              {'J7': self.plan})['J7']
        still = tension_capture.loadshare(batch, self.plan, retrofit_account)
        # The capture is bound to asset version 1; scoring it against the
        # version-3 beam bindings is rejected outright (misbound) rather than
        # silently reused, and the retrofit cannot clear the old plan.
        self.assertFalse(still['asset_binding_ok'])
        self.assertIn('misbound', still['flags'])
        self.assertFalse(still['measured_ok'])
        self.assertNotEqual(still['status'], 'ok')


class LedgerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'j7.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, name):
        kinds = {'plan-j7.json': 'plan', 'asset-j7.json': 'asset',
                 'work-j7.json': 'work', 'sling-imbalance.json': 'sling_capture',
                 'plan-j7-v2.json': 'plan', 'asset-j7-beam.json': 'asset',
                 'asset-j7-beam-static.json': 'asset'}
        return import_file(self.db, kinds[name], ROOT / 'fixtures' / name)

    def decision(self, name):
        return report(self.db)['decisions'][f'J7/{name}']['result']

    def test_normal_j7_ledger_and_capture_persist_evidence(self):
        for name in ('plan-j7.json', 'asset-j7.json', 'work-j7.json',
                     'sling-imbalance.json'):
            self.load(name)
        self.assertEqual(self.decision('索具组合适配'), '失配')
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')
        data = report(self.db)
        self.assertEqual(len(data['captures']), 1)
        self.assertEqual(data['captures'][0]['status'], 'overload')
        self.assertEqual(len(data['capture_readings']), 9)
        kgf = {r['sample_id']: r['kgf'] for r in data['capture_readings']}
        self.assertAlmostEqual(kgf['R2-off'], 1530.0, places=1)
        self.assertTrue(audit(self.db)['ok'])

    def test_overload_capture_forces_emergency_landing_when_lifted(self):
        self.load('plan-j7.json')
        self.load('asset-j7.json')
        work = fixture('work-j7.json')
        work = copy.deepcopy(work)
        work['hook_events'] = work['hook_events'][:2]  # hooked + lifted only
        path = Path(self.tmp.name) / 'work-lifted.json'
        path.write_text(json.dumps([work], ensure_ascii=False), encoding='utf-8')
        import_file(self.db, 'work', path)
        # Load is already off the ground before any meter evidence exists.
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS7')
        self.load('sling-imbalance.json')
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS7')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_plan_geometry_change_sours_the_old_measurement(self):
        # Same capture, but a new center/hook version invalidates it: the
        # resource ledger must not treat the old readings as current proof,
        # and work must not finish on the back of them.
        for name in ('plan-j7.json', 'asset-j7.json', 'work-j7.json',
                     'sling-imbalance.json'):
            self.load(name)
        moved = copy.deepcopy(fixture('plan-j7.json'))
        moved.update(id='P7-moved', version=2, signed_version=2, center=[0.0, 0.0])
        path = Path(self.tmp.name) / 'plan-moved.json'
        path.write_text(json.dumps([moved], ensure_ascii=False), encoding='utf-8')
        import_file(self.db, 'plan', path)
        # The old capture is now stale for the changed geometry.
        captures = report(self.db)['captures']
        self.assertEqual(captures[0]['status'], 'overload')  # history preserved
        self.assertEqual(self.decision('索具组合适配'), '失配')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')

    def test_retrofit_two_route_recovery_to_finished(self):
        # Old J7 overload measurement, then plan v2 + equalising beam with an
        # off-ground certificate and a new work version.
        for name in ('plan-j7.json', 'asset-j7.json', 'work-j7.json',
                     'sling-imbalance.json'):
            self.load(name)
        self.assertEqual(self.decision('索具组合适配'), '失配')
        self.load('plan-j7-v2.json')
        self.load('asset-j7-beam.json')
        work = copy.deepcopy(fixture('work-j7.json'))
        work.update(id='W9', version=3, effective_at='2026-12-01T10:10')
        work['permit_actions'] = [
            {'id': 'K1', 'at': '2026-12-01T08:55', 'action': 'release', 'plan_version': 1},
            {'id': 'K3', 'at': '2026-12-01T09:24', 'action': 'release', 'plan_version': 2},
            {'id': 'K2', 'at': '2026-12-01T09:28', 'action': 'resume', 'plan_version': 2},
        ]
        work['wind_samples'] += [
            {'id': 'G3', 'at': '2026-12-01T09:15', 'speed_mps': 14,
             'source': 'station-a', 'calibrated_until': '2026-12-31T23:59'},
            {'id': 'G4', 'at': '2026-12-01T09:17', 'speed_mps': 15,
             'source': 'station-b', 'calibrated_until': '2026-12-31T23:59'},
            {'id': 'G5', 'at': '2026-12-01T09:25', 'speed_mps': 6,
             'source': 'station-a', 'calibrated_until': '2026-12-31T23:59'},
            {'id': 'G6', 'at': '2026-12-01T09:27', 'speed_mps': 5,
             'source': 'station-b', 'calibrated_until': '2026-12-31T23:59'},
        ]
        work['hook_events'] = [
            {'id': 'H1r', 'at': '2026-12-01T09:30', 'stage': 'hook', 'witness': 'Q7'},
            {'id': 'H2r', 'at': '2026-12-01T09:35', 'stage': 'lift', 'witness': 'Q7'},
            {'id': 'H3r', 'at': '2026-12-01T09:50', 'stage': 'land', 'witness': 'Q7'},
            {'id': 'H4r', 'at': '2026-12-01T09:55', 'stage': 'sign', 'witness': 'monitor-7'},
        ]
        path = Path(self.tmp.name) / 'work-retrofit.json'
        path.write_text(json.dumps([work], ensure_ascii=False), encoding='utf-8')
        import_file(self.db, 'work', path)
        self.assertEqual(self.decision('索具组合适配'), '合格')
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        self.assertEqual(self.decision('现场风速停复工'), '放行')
        # The historic overload capture is still visible, and still an overload.
        capture_rows = report(self.db)['captures']
        self.assertEqual(capture_rows[0]['status'], 'overload')
        self.assertTrue(audit(self.db)['ok'])

    def test_static_only_beam_keeps_recovery_pending(self):
        for name in ('plan-j7.json', 'asset-j7.json', 'work-j7.json',
                     'sling-imbalance.json'):
            self.load(name)
        self.load('plan-j7-v2.json')
        self.load('asset-j7-beam-static.json')
        self.assertEqual(self.decision('索具组合适配'), '失配')


class RegressionTests(unittest.TestCase):
    def test_full_existing_fixture_paths_still_hold(self):
        # Spot-check that adding capture support did not move the original
        # two-hook J1 semantics.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = connect(Path(tmp.name) / 'j1.sqlite')
        self.addCleanup(db.close)
        for kind in ('plan', 'asset', 'work'):
            import_file(db, kind, ROOT / 'fixtures' / f'{kind}-ok.json')
        decisions = report(db)['decisions']
        self.assertEqual(decisions['J1/索具组合适配']['result'], '合格')
        self.assertEqual(decisions['J1/起落钩闭环核验']['result'], '完工')
        self.assertFalse(any(row['kind'] == 'sling_capture' for row in report(db)['sources']))


if __name__ == '__main__':
    unittest.main()
