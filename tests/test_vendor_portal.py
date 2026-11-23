"""End-to-end vendor portal capture -> certification -> asset -> work audis.

These tests start from the vocabulary of the real defects:

* two fixture pages from different revisions must not become a plausible
  table, and
* a plausible half table must not reach ``asset.rating_curve`` and must not
  release the J2 job.

They then prove the two vendor protocols (monthly signed archive versus
time-limited read lease) reach *different* conclusions from the same mixed
starting point.
"""

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from liftpermit import portal as portal_module
from liftpermit.extract import (MixedRevisionError, rating_rows,
                                snapshot_page_rows, snapshot_rows)
from liftpermit.portal import ManualClock, MockPortal, PortalTimeout
from liftpermit.resources import build_asset_event
from liftpermit.store import (audit, connect, import_file, provenance, report)
from liftpermit.vendor import (CaptureError, VendorCollector, import_page,
                               publish_snapshot, verify_archive)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def base_asset():
    return fixture('asset-j2-base.json')[0]


def tree_digest(folder):
    digest = hashlib.sha256()
    for path in sorted(Path(folder).rglob('*')):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def make_portal(name=None, scenario=None, clock=None):
    spec = fixture('vendor-portal.json')
    scenario = scenario if scenario is not None else spec['scenarios'].get(name)
    return MockPortal(spec, clock or ManualClock(), scenario)


class PortalFixtureTests(unittest.TestCase):
    def test_portal_fixture_versions_are_distinct(self):
        spec = fixture('vendor-portal.json')
        revisions = spec['documents']['TC1-rating']['revisions']
        v1 = [cell['max_kg'] for page in revisions['V1']['pages']
              for cell in page['rows']]
        v2 = [cell['max_kg'] for page in revisions['V2']['pages']
              for cell in page['rows']]
        self.assertIn(1600, v1)
        self.assertIn(1400, v1)
        self.assertIn(1200, v2)
        self.assertIn(1300, v2)
        # The dangerous non-existent table is 1600/1300: one from each.
        self.assertNotEqual(v1[:1] + v2[-1:], v2)

    def test_required_coverage_is_radius_and_boom(self):
        portal = make_portal()
        self.assertEqual(portal.required_coverage('TC1-rating'),
                         [[8, 20], [10, 25]])


class ReproducedDefectTests(unittest.TestCase):
    """The two real inputs the new code must neutralise."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_import_page_still_merges_by_page_number(self):
        db_path = self.folder / 'legacy.sqlite'
        import_page(db_path, FIXTURES / 'vendor-page-1.json')
        result = import_page(db_path, FIXTURES / 'vendor-v2-page-2.json')
        self.assertEqual([row['max_kg'] for row in result['rows']], [1600, 1300])

    def test_snapshot_never_merges_the_same_two_inputs(self):
        db = connect(self.folder / 'e2e.sqlite')
        try:
            import_page(self.folder / 'e2e.sqlite',
                        FIXTURES / 'vendor-page-1.json')
            import_page(self.folder / 'e2e.sqlite',
                        FIXTURES / 'vendor-v2-page-2.json')
            self.assertEqual([row['max_kg'] for row in rating_rows(db, 'TC1-rating')],
                             [1600, 1300])
            portal = make_portal()
            result = VendorCollector(db, portal).capture()
            self.assertEqual(result['state'], 'certified')
            self.assertEqual([row['max_kg'] for row in result['rows']], [1200, 1300])
            self.assertTrue(all(row['max_kg'] != 1600 for row in result['rows']))
        finally:
            db.close()

    def test_legacy_mixed_asset_still_releases_off_the_record_layout(self):
        # Documented reproduction: an asset file hand-built from the mixed
        # table embeds no provenance, so the legacy path still releases it.
        # This is exactly the manual fabrication the portal path removes.
        db = connect(self.folder / 'legacy-mixed.sqlite')
        try:
            import_file(db, 'plan', FIXTURES / 'plan-j2.json')
            import_file(db, 'asset', FIXTURES / 'asset-j2-mixed.json')
            import_file(db, 'work', FIXTURES / 'work-j2.json')
            entities = {row['subsystem']: row['state']
                        for row in report(db)['entities']}
            self.assertEqual(entities['现场许可与作业账'], 'finished')
            self.assertEqual(provenance(db, 'J2')['provenance'], 'attested')
            self.assertEqual(provenance(db, 'J2')['reason'],
                             'legacy_asset_without_provenance')
        finally:
            db.close()

    def test_embedded_curve_cannot_attest_without_snapshot(self):
        db = connect(self.folder / 'forged.sqlite')
        try:
            asset = fixture('asset-j2-mixed.json')[0]
            asset['rating_provenance'] = {'status': 'observed',
                                          'snapshot_id': 'TC1-rating/V2/does-not-exist'}
            path = self.folder / 'forged-asset.json'
            path.write_text(json.dumps([asset], ensure_ascii=False),
                            encoding='utf-8')
            import_file(db, 'plan', FIXTURES / 'plan-j2.json')
            import_file(db, 'asset', path)
            import_file(db, 'work', FIXTURES / 'work-j2.json')
            self.assertTrue(provenance(db, 'J2')['blocked'])
            entities = {row['subsystem']: row['state']
                        for row in report(db)['entities']}
            self.assertEqual(entities['设备与人员资格账'], 'frozen')
            self.assertEqual(entities['现场许可与作业账'], 'pending')
        finally:
            db.close()

    def test_absent_provenance_is_never_published(self):
        db = connect(self.folder / 'absent.sqlite')
        try:
            asset = base_asset()
            asset['rating_provenance'] = {'status': 'absent'}
            asset['crane']['rating_curve'] = [
                {'radius_m': 8, 'boom_m': 20, 'max_kg': 9999}]
            path = self.folder / 'absent.json'
            path.write_text(json.dumps([asset], ensure_ascii=False),
                            encoding='utf-8')
            import_file(db, 'asset', path)
            self.assertTrue(provenance(db, 'J2')['blocked'])
            self.assertEqual(db.execute(
                'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 0)
        finally:
            db.close()


class CaptureProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db = connect(self.folder / 'capture.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def collect(self, name=None, scenario=None, **kwargs):
        portal = make_portal(name, scenario=scenario, clock=ManualClock())
        collector = VendorCollector(self.db, portal, **kwargs)
        return portal, collector.capture()

    def test_happy_path_certifies_and_publishes_full_curve(self):
        _, result = self.collect()
        self.assertEqual(result['state'], 'certified')
        self.assertEqual([r['max_kg'] for r in result['rows']], [1200, 1300])
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertTrue(published['published'])
        self.assertEqual([r['max_kg'] for r in published['rows']], [1200, 1300])

    def test_pending_snapshot_never_writes_asset_curve(self):
        _, result = self.collect('page_gone')
        self.assertEqual(result['state'], 'pending')
        self.assertEqual([gap['page_no'] for gap in result['gaps']], [2])
        # The literal raw page captured so far is available for review...
        self.assertEqual([p['page_no'] for p in result['raw_pages']], [1])
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])
        self.assertEqual(published['missing_pages'], [2])
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 0)
        with self.assertRaises(ValueError):
            snapshot_rows(self.db, result['snapshot_id'])

    def test_retryable_timeout_recovers_to_full_snapshot(self):
        _, result = self.collect('transient_timeout')
        self.assertEqual(result['state'], 'certified')
        attempts = {p['page_no']: p['attempts'] for p in result['pages']}
        self.assertEqual(attempts[1], 2)
        self.assertEqual(attempts[2], 1)

    def test_exhausted_retry_reports_gap_not_partial_curve(self):
        sleeps = []
        _, result = self.collect('persistent_503',
                                 sleep=sleeps.append)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['missing_pages'], [2])
        self.assertTrue(sleeps)
        self.assertIsNone(result['rows'])

    def test_out_of_order_and_duplicate_delivery_is_normalised(self):
        _, result = self.collect('out_of_order_duplicate')
        self.assertEqual(result['state'], 'certified')
        self.assertEqual(result['captured_pages'], [1, 2])
        self.assertEqual(result['duplicates'], [{'page_no': 1, 'delivery': '1'}])

    def test_tampered_page_fails_signature_and_stays_pending(self):
        _, result = self.collect('tamper_page')
        self.assertEqual(result['state'], 'pending')
        self.assertIn({'page_no': 2, 'code': 'not_captured'},
                      [{'page_no': g['page_no'], 'code': g['code']}
                       for g in result['gaps']])
        self.assertTrue(any(f['code'] in ('page_digest_mismatch',
                                          'page_signature_invalid')
                            for f in result['failures']))

    def test_resume_uses_etag_cache_and_streams_without_refetch(self):
        portal = make_portal()
        collector = VendorCollector(self.db, portal)
        first = collector.capture()
        self.assertEqual(first['state'], 'certified')
        second = collector.capture()
        self.assertEqual(second['state'], 'certified')
        self.assertTrue(all(p.get('reused_cache') for p in second['pages']))
        self.assertEqual([p['sha256'] for p in second['pages']],
                         [p['sha256'] for p in first['pages']])

    def test_stale_304_for_old_revision_is_not_accepted(self):
        spec = fixture('vendor-portal.json')
        fresh = MockPortal(spec, ManualClock())
        result = VendorCollector(self.db, fresh).capture()
        self.assertEqual(result['state'], 'certified')
        # Rewrite the cached V2 page-1 ETag to V1's page-1 ETag, as a stale
        # local cache might.  The portal then answers 304 for V2 page 1 while
        # meaning V1's bytes.  Reusing the cache would silently splice an old
        # revision into the table.
        v1_entry = fresh._entry('TC1-rating', 'V1',
                                fresh.page_config('TC1-rating', 'V1', 1))
        self.db.execute(
            "UPDATE vendor_capture SET etag=? WHERE revision='V2' AND page_no=1",
            (v1_entry['etag'],))
        self.db.commit()
        portal = make_portal('stale_etag_cache')
        second = VendorCollector(self.db, portal).capture()
        self.assertEqual(second['state'], 'pending')
        self.assertTrue(any(f['code'] == 'stale_etag'
                            for f in second['failures']))
        self.assertEqual([p['page_no'] for p in second['raw_pages']], [2])
        published = publish_snapshot(self.db, second['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])
        self.assertNotEqual([r['max_kg'] for r in (second['rows'] or [])],
                            [1600, 1300])

    def test_growing_page_count_is_rediscovered(self):
        _, result = self.collect('grow_to_three_pages')
        self.assertEqual(result['expected_pages'], 3)
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['missing_pages'], [3])
        # Page 3 has no link in the current listing, so no further pass can
        # produce it: the scan converges on an explicit gap.
        self.assertTrue(result['converged'])
        self.assertEqual([g['code'] for g in result['gaps']
                          if g['page_no'] == 3], ['missing_link'])

    def test_lease_expiry_midscan_is_pending_with_partial_pages_only(self):
        _, result = self.collect('lease_expires_midscan')
        self.assertEqual(result['state'], 'pending')
        self.assertEqual(result['missing_pages'], [2])
        self.assertTrue(any(f['code'] == 'lease_expired'
                            for f in result['failures']))
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 0)

    def test_revision_flip_midscan_is_ambiguous_and_never_published(self):
        _, result = self.collect('flip_to_v1_midscan')
        self.assertEqual(result['state'], 'ambiguous')
        self.assertTrue(result['revision_drift'])
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 0)

    def test_applicable_radius_and_boom_must_be_covered(self):
        portal = make_portal()
        portal.documents['TC1-rating']['required_coverage'] = [[8, 20], [10, 25],
                                                                [12, 30]]
        result = VendorCollector(self.db, portal).capture(
            applicable={'radius_m': 10, 'boom_m': 25})
        self.assertEqual(result['state'], 'certified')
        self.assertTrue(result['coverage']['applicable_covered'])
        # But a plan asking for an uncovered cell cannot be certified.
        portal2 = make_portal()
        portal2.documents['TC1-rating']['required_coverage'] = [[8, 20], [10, 25],
                                                                [12, 30]]
        result2 = VendorCollector(self.db, portal2).capture(
            applicable={'radius_m': 12, 'boom_m': 30})
        self.assertEqual(result2['state'], 'pending')
        self.assertFalse(result2['coverage']['applicable_covered'])

    def test_two_revisions_coexist_without_overwriting(self):
        portal = make_portal()
        archive = verify_archive(self.db, portal, 'TC1-rating', 'V1')
        lease = VendorCollector(self.db, portal).capture()
        rows = self.db.execute(
            'SELECT revision, COUNT(*) FROM vendor_snapshot '
            'GROUP BY revision ORDER BY revision').fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [('V1', 1), ('V2', 1)])
        self.assertEqual([r['max_kg'] for r in archive['rows'][1]],
                         [1600, 1500])
        self.assertEqual([r['max_kg'] for r in lease['rows']], [1200, 1300])
        # The two page sets remain distinct in the immutable capture table.
        v1_pages = self.db.execute(
            "SELECT page_no, sha256 FROM vendor_capture WHERE revision='V1' "
            'ORDER BY page_no').fetchall()
        v2_pages = self.db.execute(
            "SELECT page_no, sha256 FROM vendor_capture WHERE revision='V2' "
            'ORDER BY page_no').fetchall()
        self.assertNotEqual([p['sha256'] for p in v1_pages],
                            [p['sha256'] for p in v2_pages])

    def test_snapshot_pages_are_revision_pure(self):
        _, result = self.collect()
        pages = snapshot_page_rows(self.db, result['snapshot_id'])
        self.assertEqual({p['revision'] for p in pages}, {'V2'})
        self.assertEqual([p['page_no'] for p in pages], [1, 2])

    def test_certified_snapshot_can_be_reverified_at_publish_time(self):
        # A row edited after certification must fail the publish-time
        # re-hash, so a later database write cannot smuggle a half table in.
        _, result = self.collect()
        page = self.db.execute(
            "SELECT raw FROM vendor_capture WHERE revision='V2' AND page_no=2"
        ).fetchone()
        tampered = page['raw'].replace(b'"max_kg":1300', b'"max_kg":1500')
        self.assertNotEqual(tampered, page['raw'])
        self.db.execute(
            "UPDATE vendor_capture SET raw=? WHERE revision='V2' AND page_no=2",
            (tampered,))
        self.db.commit()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])
        self.assertIn('page_digest_changed',
                      [p['code'] for p in published['problems']])
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 0)

    def test_asset_curve_must_match_snapshot_exactly(self):
        _, result = self.collect()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        asset = build_asset_event(
            base_asset(), published['rows'],
            snapshot_id=result['snapshot_id'], doc_id='TC1-rating',
            revision='V2')
        # Same radius/boom coverage but a substituted capacity: provenance is
        # valid, yet the curve no longer equals the certified snapshot.
        asset['crane']['rating_curve'] = [
            {'radius_m': 8, 'boom_m': 20, 'max_kg': 1600},
            {'radius_m': 10, 'boom_m': 25, 'max_kg': 1300}]
        path = self.folder / 'mismatch.json'
        path.write_text(json.dumps([asset], ensure_ascii=False),
                        encoding='utf-8')
        import_file(self.db, 'asset', path)
        self.assertTrue(provenance(self.db, 'J2')['blocked'])
        self.assertEqual(provenance(self.db, 'J2')['provenance'],
                         'uncertified_provenance')

    def test_work_ledger_names_the_unverified_curve_block(self):
        # An asset whose curve has no certified snapshot must make the work
        # ledger report an explicit unverified-curve block, not a bare idle
        # ``pending`` that could be mistaken for "just not started".
        asset = fixture('asset-j2-mixed.json')[0]
        asset['rating_provenance'] = {'status': 'absent'}
        path = self.folder / 'absent.json'
        path.write_text(json.dumps([asset], ensure_ascii=False),
                        encoding='utf-8')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        import_file(self.db, 'asset', path)
        import_file(self.db, 'work', FIXTURES / 'work-j2.json')
        from liftpermit import plans, resources, rules, work
        p = plans.evaluate([json.loads(row['body']) for row in self.db.execute(
            'SELECT body FROM events WHERE kind="plan"')])
        a = resources.evaluate([json.loads(row['body']) for row in self.db.execute(
            'SELECT body FROM events WHERE kind="asset"')], p, self.db)
        w = work.evaluate([json.loads(row['body']) for row in self.db.execute(
            'SELECT body FROM events WHERE kind="work"')], p, a)
        self.assertFalse(a['J2']['release_allowed'])
        self.assertEqual(w['J2']['status'], 'pending')
        self.assertEqual(w['J2']['blocked_by'], 'unverified')
        self.assertEqual(w['J2']['decisions']['现场风速停复工'][0], '停工')


class TwoProtocolTests(unittest.TestCase):
    """Archive (V1) and lease (V2) start from the same mix and diverge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db = connect(self.folder / 'protocols.sqlite')
        self.portal = make_portal()
        # The common mixed starting point: legacy pages merged by page_no.
        import_page(self.folder / 'protocols.sqlite',
                    FIXTURES / 'vendor-page-1.json')
        import_page(self.folder / 'protocols.sqlite',
                    FIXTURES / 'vendor-v2-page-2.json')
        self.assertEqual([r['max_kg'] for r in rating_rows(self.db, 'TC1-rating')],
                         [1600, 1300])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _run_j2(self, published, *, revision):
        asset = build_asset_event(base_asset(), published['rows'],
                                  snapshot_id=published['snapshot_id'],
                                  doc_id='TC1-rating', revision=revision)
        path = self.folder / f'asset-{revision}.json'
        path.write_text(json.dumps([asset], ensure_ascii=False),
                        encoding='utf-8')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        import_file(self.db, 'asset', path)
        import_file(self.db, 'work', FIXTURES / 'work-j2.json')
        return {row['subsystem']: row['state']
                for row in report(self.db)['entities']}

    def test_archive_proves_historical_v1_and_can_release_its_own_job(self):
        archive = verify_archive(self.db, self.portal, 'TC1-rating', 'V1')
        self.assertEqual(archive['state'], 'certified')
        self.assertFalse(archive['is_current_revision'])
        self.assertEqual(archive['archive_id'], 'TC1-rating-2026-11')
        self.assertEqual([r['max_kg'] for r in
                          archive['rows'][1] + archive['rows'][2]],
                         [1600, 1500, 1400])
        published = publish_snapshot(self.db, archive['snapshot_id'], 'TC1', 'J2')
        self.assertTrue(published['published'])
        entities = self._run_j2(published, revision='V1')
        # V1 genuinely rates 8m/20m at 1600 kg, so a J2 job flying V1's real
        # curve can finish -- but it does so on V1 evidence, not V2.
        self.assertEqual(entities['设备与人员资格账'], 'reserved')
        self.assertEqual(entities['现场许可与作业账'], 'finished')
        self.assertEqual(provenance(self.db, 'J2')['provenance'], 'historical')
        # The released asset still carries V1's real rows, never [1600,1300].
        asset = json.loads(self.db.execute(
            'SELECT body FROM events WHERE kind="asset"').fetchone()[0])
        self.assertEqual(asset['crane']['rating_curve'],
                         published['rows'])
        self.assertNotEqual([r['max_kg'] for r in asset['crane']['rating_curve']],
                            [1600, 1300])

    def test_archive_cannot_impersonate_current_v2(self):
        with self.assertRaises(CaptureError) as caught:
            verify_archive(self.db, self.portal, 'TC1-rating', 'V2')
        self.assertEqual(caught.exception.code, 'archive_not_published')

    def test_lease_proves_current_v2_and_blocks_j2(self):
        lease = VendorCollector(self.db, self.portal).capture()
        self.assertEqual(lease['state'], 'certified')
        self.assertEqual([r['max_kg'] for r in lease['rows']], [1200, 1300])
        published = publish_snapshot(self.db, lease['snapshot_id'], 'TC1', 'J2')
        entities = self._run_j2(published, revision='V2')
        self.assertEqual(entities['设备与人员资格账'], 'frozen')
        self.assertEqual(entities['现场许可与作业账'], 'pending')
        self.assertNotEqual(entities['现场许可与作业账'], 'finished')

    def test_two_protocols_reach_different_conclusions(self):
        archive = verify_archive(self.db, self.portal, 'TC1-rating', 'V1')
        lease = VendorCollector(self.db, self.portal).capture()
        self.assertEqual(archive['revision'], 'V1')
        self.assertEqual(lease['revision'], 'V2')
        self.assertEqual([r['max_kg'] for r in archive['rows'][1]], [1600, 1500])
        self.assertEqual([r['max_kg'] for r in lease['rows']], [1200, 1300])
        self.assertEqual(archive['coverage']['missing_pages'], [])
        self.assertEqual(lease['coverage']['missing_pages'], [])
        self.assertNotEqual(archive['snapshot_id'], lease['snapshot_id'])
        # The archive holds V1 evidence for V1's own revision; the lease holds
        # V2 for the live revision.  Neither is the mixed 1600/1300 table.
        for result in (archive, lease):
            if result is archive:
                flat = [row['max_kg'] for rows in archive['rows'].values()
                        for row in rows]
            else:
                flat = [row['max_kg'] for row in lease['rows']]
            self.assertNotEqual(flat, [1600, 1300])


class EndToEndAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db_path = self.folder / 'e2e.sqlite'
        self.db = connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def write_asset(self, published, *, revision, name='asset.json'):
        asset = build_asset_event(base_asset(), published['rows'],
                                  snapshot_id=published['snapshot_id'],
                                  doc_id='TC1-rating', revision=revision)
        path = self.folder / name
        path.write_text(json.dumps([asset], ensure_ascii=False),
                        encoding='utf-8')
        return path

    def test_certified_curve_is_frozen_for_j2_and_audit_chain_is_complete(self):
        portal = make_portal()
        result = VendorCollector(self.db, portal).capture()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        import_file(self.db, 'asset', self.write_asset(published, revision='V2'))
        import_file(self.db, 'work', FIXTURES / 'work-j2.json')
        entities = {row['subsystem']: row['state']
                    for row in report(self.db)['entities']}
        self.assertEqual(entities['设备与人员资格账'], 'frozen')
        self.assertEqual(entities['现场许可与作业账'], 'pending')

    def test_provenance_chain_from_raw_bytes_to_asset_and_work(self):
        portal = make_portal()
        result = VendorCollector(self.db, portal).capture()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        asset_path = self.write_asset(published, revision='V2')
        import_file(self.db, 'asset', asset_path)
        import_file(self.db, 'work', FIXTURES / 'work-j2.json')

        # 1. The asset curve equals the certified snapshot rows exactly.
        asset_event = json.loads(
            self.db.execute('SELECT body FROM events WHERE kind="asset"')
            .fetchone()[0])
        self.assertEqual(asset_event['crane']['rating_curve'], result['rows'])
        # 2. Every page digest in the asset provenance exists as raw bytes.
        for page in result['raw_pages']:
            stored = self.db.execute(
                'SELECT raw FROM vendor_capture WHERE sha256=?',
                (page['sha256'],)).fetchone()
            self.assertIsNotNone(stored)
            self.assertEqual(hashlib.sha256(bytes(stored[0])).hexdigest(),
                             page['sha256'])
            self.assertEqual(page['raw'], bytes(stored[0]))
        # 3. The published asset curve's evidence names the same digests.
        curve = self.db.execute(
            'SELECT evidence FROM asset_rating_curve').fetchone()
        evidence = json.loads(curve['evidence'])
        self.assertEqual(evidence['snapshot_sha256'],
                         self.db.execute(
                             'SELECT raw_sha256 FROM vendor_snapshot '
                             'WHERE snapshot_id=?',
                             (result['snapshot_id'],)).fetchone()[0])
        self.assertEqual(evidence['page_sha256'],
                         {str(p['page_no']): p['sha256']
                          for p in result['raw_pages']})
        # 4. The work decision evidence names the asset event.
        decision = report(self.db)['decisions']['J2/塔机额定载荷复核']
        self.assertIn('A-J2-E2E', decision['evidence'])
        self.assertEqual(decision['result'], '超限或检验失效')

    def test_import_rollback_keeps_only_complete_snapshot(self):
        portal = make_portal()
        result = VendorCollector(self.db, portal).capture()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        asset_path = self.write_asset(published, revision='V2')
        import_file(self.db, 'asset', asset_path)
        before = report(self.db)
        # A colliding event id with different content rolls the whole batch
        # back, leaving the certified snapshot and the prior asset intact.
        asset_event = json.loads(asset_path.read_text(encoding='utf-8'))[0]
        asset_event['crane']['rating_curve'] = [
            {'radius_m': 8, 'boom_m': 20, 'max_kg': 1600}]
        bad_path = self.folder / 'bad-asset.json'
        bad_path.write_text(json.dumps([asset_event], ensure_ascii=False),
                            encoding='utf-8')
        with self.assertRaises(ValueError):
            import_file(self.db, 'asset', bad_path)
        self.assertEqual(report(self.db), before)
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM asset_rating_curve').fetchone()[0], 1)

    def test_scan_interrupted_then_resumed_converges(self):
        portal = make_portal()
        resp = portal.fetch_page
        calls = {'n': 0}

        def flaky(url, etag=None, lease_id=None):
            calls['n'] += 1
            if calls['n'] == 2:
                raise PortalTimeout('injected')
            return resp(url, etag=etag, lease_id=lease_id)

        portal.fetch_page = flaky
        collector = VendorCollector(self.db, portal)
        result = collector.capture()
        self.assertEqual(result['state'], 'certified')
        # Page 1 fetched, page 2 timed out then was fetched on retry.
        self.assertEqual(calls['n'], 3)
        self.assertEqual(result['captured_pages'], [1, 2])
        attempts = {p['page_no']: p['attempts'] for p in result['pages']}
        self.assertEqual(attempts[2], 2)

    def test_audit_replays_original_bytes(self):
        portal = make_portal()
        result = VendorCollector(self.db, portal).capture()
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        import_file(self.db, 'plan', FIXTURES / 'plan-j2.json')
        import_file(self.db, 'asset', self.write_asset(published, revision='V2'))
        import_file(self.db, 'work', FIXTURES / 'work-j2.json')
        self.assertTrue(audit(self.db)['ok'])


class VersionConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.db = connect(self.folder / 'conv.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_undecidable_when_revision_keeps_changing(self):
        portal = make_portal('flip_after_two_fetches')
        result = VendorCollector(self.db, portal,
                                 retry_policy={'max_passes': 2}).capture()
        self.assertEqual(result['state'], 'ambiguous')
        self.assertFalse(result['certified'])
        self.assertTrue(result['undecidable'])
        published = publish_snapshot(self.db, result['snapshot_id'], 'TC1', 'J2')
        self.assertFalse(published['published'])

    def test_stable_current_revision_converges(self):
        portal = make_portal()
        result = VendorCollector(self.db, portal).capture()
        self.assertTrue(result['converged'])
        self.assertLessEqual(result['passes'], 2)
        self.assertEqual(result['state'], 'certified')


if __name__ == '__main__':
    unittest.main()
