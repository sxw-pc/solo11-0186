import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from liftpermit.vendor import import_page
from liftpermit.store import connect,import_file,report


class VendorSeedTests(unittest.TestCase):
    def test_single_page_rows(self):
        with TemporaryDirectory() as folder:
            result=import_page(Path(folder)/'state.sqlite','fixtures/vendor-page-1.json')
            self.assertEqual(result['rows'][0]['max_kg'],1600)

    def test_mixed_revisions_provisionally_merge(self):
        with TemporaryDirectory() as folder:
            db=Path(folder)/'state.sqlite'
            import_page(db,'fixtures/vendor-page-1.json')
            result=import_page(db,'fixtures/vendor-v2-page-2.json')
            self.assertEqual([row['max_kg'] for row in result['rows']],[1600,1300])

    def test_mixed_asset_can_release_j2(self):
        with TemporaryDirectory() as folder:
            db=connect(Path(folder)/'state.sqlite')
            try:
                import_file(db,'plan','fixtures/plan-j2.json')
                import_file(db,'asset','fixtures/asset-j2-mixed.json')
                import_file(db,'work','fixtures/work-j2.json')
                self.assertEqual(next(x['state'] for x in report(db)['entities']
                                      if x['subsystem']=='现场许可与作业账'),'finished')
            finally:db.close()

    def test_current_v2_asset_freezes_j2(self):
        with TemporaryDirectory() as folder:
            db=connect(Path(folder)/'state.sqlite')
            try:
                import_file(db,'plan','fixtures/plan-j2.json')
                import_file(db,'asset','fixtures/asset-j2-v2.json')
                import_file(db,'work','fixtures/work-j2.json')
                self.assertEqual(next(x['state'] for x in report(db)['entities']
                                      if x['subsystem']=='现场许可与作业账'),'pending')
            finally:db.close()
