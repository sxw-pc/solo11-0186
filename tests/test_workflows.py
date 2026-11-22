import json
import tempfile
import unittest
from pathlib import Path
import liftpermit
from liftpermit.store import connect,import_file,report,audit

ROOT=Path(__file__).resolve().parents[1]
class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.tmp.name)/'state.sqlite')
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def load(self,command,file): return import_file(self.db,IMPORTS[command],ROOT/'fixtures'/file)
    def test_normal_decisions_and_replay(self):
        for cmd,file in DEMO: self.load(cmd,file)
        actual=report(self.db)
        self.assertGreaterEqual(len(actual['decisions']),10)
        self.assertTrue(audit(self.db)['ok'])
        self.assertTrue(all(d['evidence'] for d in actual['decisions'].values()))
        print('module file', liftpermit.__file__)
        self.assertTrue(str(Path(liftpermit.__file__).resolve()).startswith(str(ROOT)))
    def test_idempotency_and_conflict_rollback(self):
        cmd,file=DEMO[0]
        first=self.load(cmd,file)
        self.assertTrue(self.load(cmd,file)['idempotent'])
        original=report(self.db)
        data=json.loads((ROOT/'fixtures'/file).read_text(encoding='utf-8'))
        data[0]['value']='changed'
        conflict=Path(self.tmp.name)/'conflict.json'
        conflict.write_text(json.dumps(data),encoding='utf-8')
        with self.assertRaises(ValueError): import_file(self.db,IMPORTS[cmd],conflict)
        self.assertEqual(original,report(self.db))
    def test_boundaries_change_outcomes(self):
        for cmd,file in DEMO: self.load(cmd,file)
        before=report(self.db)['decisions']
        for cmd,file in BAD: self.load(cmd,file)
        after=report(self.db)['decisions']
        changed=[name for name in before if before[name]['result']!=after[name]['result']]
        self.assertGreaterEqual(len(changed),3,changed)
        self.assertTrue(audit(self.db)['ok'])
    def test_six_distinct_business_entities(self):
        for cmd,file in DEMO: self.load(cmd,file)
        records=report(self.db)['business_records']
        self.assertGreaterEqual(len(set((r['subsystem'],r['entity_type']) for r in records)),6)
        self.assertTrue(all(r['source_event'] for r in records))
    def test_audit_detects_materialized_state_tampering(self):
        for cmd,file in DEMO: self.load(cmd,file)
        name=next(iter(report(self.db)['decisions']))
        self.db.execute('UPDATE decisions SET result=? WHERE name=?',('FORGED',name))
        self.db.commit()
        self.assertFalse(audit(self.db)['ok'])
    def test_expected_normal_conclusions(self):
        for cmd,file in DEMO: self.load(cmd,file)
        decisions=report(self.db)['decisions']
        for name,expected in NORMAL.items():
            with self.subTest(name=name): self.assertEqual(decisions[name]['result'],expected)
    def test_each_subsystem_changes_own_decision(self):
        for cmd,file,decision in CROSS:
            with self.subTest(decision=decision):
                sample=connect(':memory:')
                for normal_cmd,normal_file in DEMO:
                    import_file(sample,IMPORTS[normal_cmd],ROOT/'fixtures'/normal_file)
                before=report(sample)['decisions'][decision]['result']
                import_file(sample,IMPORTS[cmd],ROOT/'fixtures'/file)
                after=report(sample)['decisions'][decision]['result']
                self.assertNotEqual(before,after)
                sample.close()

IMPORTS={'plan-import': 'plan', 'asset-import': 'asset', 'work-import': 'work'}
DEMO=[('plan-import', 'plan-ok.json'), ('asset-import', 'asset-ok.json'), ('work-import', 'work-ok.json')]
BAD=[('plan-import', 'plan-bad.json'), ('asset-import', 'asset-bad.json'), ('work-import', 'work-bad.json')]
NORMAL={'吊物重量版本核定': '有效', '吊点重心方案签认': '签认', '回转路径禁区核验': '可通行', '塔机额定载荷复核': '合格', '索具组合适配': '合格', '指挥资质排班确认': '覆盖', '现场风速停复工': '放行', '起落钩闭环核验': '完工'}
CROSS=[('plan-import', 'plan-bad.json', '现场风速停复工'), ('asset-import', 'asset-bad.json', '现场风速停复工'), ('work-import', 'work-bad.json', '现场风速停复工')]
