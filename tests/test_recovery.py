"""Cross-ledger failure and recovery contracts for the tower crane workflow."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
KINDS = ('plan', 'asset', 'work')


def sample(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))[0]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'recovery.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, kind, name):
        return import_file(self.db, kind, ROOT / 'fixtures' / name)

    def import_event(self, event):
        path = Path(self.tmp.name) / f"{event['id']}.json"
        path.write_text(json.dumps([event], ensure_ascii=False), encoding='utf-8')
        import_file(self.db, event['kind'], path)

    def decision(self, name):
        return report(self.db)['decisions'][f'J1/{name}']['result']

    def normal(self):
        for kind in KINDS:
            self.load(kind, f'{kind}-ok.json')

    def bad(self):
        for kind in KINDS:
            self.load(kind, f'{kind}-bad.json')

    def test_bad_then_recovered_three_ledger_path(self):
        self.normal()
        self.bad()
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地')
        for kind in KINDS:
            self.load(kind, f'{kind}-recover.json')
        self.assertEqual(self.decision('现场风速停复工'), '放行')
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        count = self.db.execute("SELECT COUNT(*) FROM decision_history WHERE name='J1/现场风速停复工'").fetchone()[0]
        self.assertGreaterEqual(count, 3)
        self.assertTrue(audit(self.db)['ok'])

    def test_resume_cannot_reuse_old_plan_version(self):
        self.normal()
        self.bad()
        self.load('plan', 'plan-recover.json')
        self.load('asset', 'asset-recover.json')
        stale = copy.deepcopy(sample('work-recover.json'))
        stale.update(id='W3-stale', version=3)
        stale['permit_actions'][-1]['plan_version'] = 2
        self.import_event(stale)
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_wind_clear_and_same_plan_allows_authorized_resume(self):
        self.normal()
        recovered = copy.deepcopy(sample('work-recover.json'))
        recovered.update(id='W2', version=2)
        for action in recovered['permit_actions']:
            action['plan_version'] = 1
        self.import_event(recovered)
        self.assertEqual(self.decision('现场风速停复工'), '放行')
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')

    def test_new_release_cannot_retroactively_authorize_old_hooks(self):
        self.normal()
        self.bad()
        self.load('plan', 'plan-recover.json')
        self.load('asset', 'asset-recover.json')
        stale = copy.deepcopy(sample('work-recover.json'))
        stale['id'] = 'W3-old-hooks'
        stale['hook_events'] = sample('work-ok.json')['hook_events']
        self.import_event(stale)
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')


if __name__ == '__main__':
    unittest.main()
