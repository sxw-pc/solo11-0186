"""离线值守采录与连续交接链：相同显示刷卡时间不能推出无缝覆盖。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from liftpermit import handoff_capture, plans, resources, work
from liftpermit.store import audit, connect, import_file, report

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-12-01"


def sample(name):
    return json.loads((ROOT / 'fixtures' / name).read_text(encoding='utf-8'))[0]


def at(hm):
    return f"{DAY}T{hm}"


def handoff(**over):
    event = copy.deepcopy(sample('commander-handoff.json'))
    event.update(**over)
    return event


def add_token_video(event):
    """已批准补证一：双方签名的交接令牌 + 监理签章视频索引证明 09:29–09:31 同场。"""
    token = {"id": "T1", "from": "Q1", "to": "Q2", "issued_at": at("09:30"),
             "overlap_start": at("09:29"), "overlap_end": at("09:31")}
    token["signatures"] = {p: handoff_capture.token_signature(p, token)
                           for p in ("Q1", "Q2")}
    video = {"id": "V1", "monitor": "monitor-1",
             "segments": [{"start": at("09:29"), "end": at("09:31"),
                           "present": ["Q1", "Q2"]}]}
    video["signature"] = handoff_capture.video_signature(video)
    event["tokens"] = [token]
    event["witnesses"] = [video]
    return event


def add_gapped_video(event):
    """视频缺段：交接时段没有影像，同场无法证明。"""
    token = {"id": "T1", "from": "Q1", "to": "Q2", "issued_at": at("09:30"),
             "overlap_start": at("09:29"), "overlap_end": at("09:31")}
    token["signatures"] = {p: handoff_capture.token_signature(p, token)
                           for p in ("Q1", "Q2")}
    video = {"id": "V2", "monitor": "monitor-1",
             "segments": [{"start": at("09:20"), "end": at("09:26"),
                           "present": ["Q1"]}]}
    video["signature"] = handoff_capture.video_signature(video)
    event["tokens"] = [token]
    event["witnesses"] = [video]
    return event


def resume_order(**over):
    order = {"id": "RO1", "by": "Q2", "at": at("09:34"), "plan_version": 1}
    order.update(over)
    return order


def handoff_asset():
    asset = copy.deepcopy(sample('asset-ok.json'))
    asset.update(id='AH1', version=1)
    asset['commander']['shifts'] = [{'start': at('08:00'), 'end': at('09:30')}]
    return asset


def handoff_work(**over):
    event = copy.deepcopy(sample('work-ok.json'))
    event.update(id='WH1', version=1)
    event['hook_events'] = [
        {"id": "H1", "at": at("09:10"), "stage": "hook", "witness": "Q1"},
        {"id": "H2", "at": at("09:25"), "stage": "lift", "witness": "Q1"},
        {"id": "H3", "at": at("09:40"), "stage": "land", "witness": "Q2"},
        {"id": "H4", "at": at("09:45"), "stage": "sign", "witness": "monitor-1"},
    ]
    event.update(**over)
    return event


def evaluate_all(handoff_events, work_event=None, plan_event=None):
    p = plans.evaluate([plan_event or sample('plan-ok.json')])
    h = handoff_capture.evaluate(handoff_events, p)
    a = resources.evaluate([handoff_asset()], p, h)
    w = work.evaluate([work_event or handoff_work()], p, a)
    return p, h, a, w


class HandoffChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def import_event(self, event):
        path = Path(self.tmp.name) / f"{event['id']}.json"
        path.write_text(json.dumps([event], ensure_ascii=False), encoding='utf-8')
        return import_file(self.db, event['kind'], path)

    def stage(self, *handoffs, work_event=None):
        self.import_event(sample('plan-ok.json'))
        self.import_event(handoff_asset())
        self.import_event(work_event or handoff_work())
        for event in handoffs:
            self.import_event(event)

    def decision(self, name):
        return report(self.db)['decisions'][f'J1/{name}']['result']

    def work_state(self):
        return self.db.execute(
            "SELECT state FROM entity_state WHERE subsystem='现场许可与作业账'").fetchone()[0]

    def history(self, name):
        return [r[0] for r in self.db.execute(
            'SELECT result FROM decision_history WHERE name=? ORDER BY revision',
            (f'J1/{name}',))]

    def compact_history(self, name):
        compact = []
        for result in self.history(name):
            if not compact or compact[-1] != result:
                compact.append(result)
        return compact

    def test_displayed_0930_alone_leaves_unverified_gap(self):
        self.stage(handoff())
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('值守交接链核验'), '中断')
        self.assertEqual(self.decision('交接重叠证明'), '待核')
        self.assertEqual(self.decision('刷卡采录验签'), '通过')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        self.assertEqual(self.work_state(), 'suspended')
        _, h, a, _ = evaluate_all([handoff()])
        chain = h['J1']
        self.assertEqual(chain['pending'],
                         [{'start': at('09:27'), 'end': at('09:33')}])
        self.assertEqual(chain['workable'], [
            {'start': at('09:00'), 'end': at('09:27')},
            {'start': at('09:33'), 'end': at('10:00')}])
        self.assertTrue(all(r['status'] == 'verified' for r in chain['swipe_reviews']))
        self.assertFalse(a['J1']['staff_ok'])
        # 错误候选写法：把两段显示时间合并成一条 09:00–10:00 班次，旧 _covers 会判覆盖
        merged = [{'start': at('09:00'), 'end': at('10:00')}]
        self.assertTrue(resources._covers(at('09:00'), at('10:00'), merged))
        self.assertTrue(audit(self.db)['ok'])

    def test_token_and_monitor_video_complete_chain(self):
        self.stage(handoff(), add_token_video(handoff(id='HO2', version=2,
                                                    effective_at=at('09:36'))))
        self.assertEqual(self.decision('指挥资质排班确认'), '覆盖')
        self.assertEqual(self.decision('值守交接链核验'), '连续')
        self.assertEqual(self.decision('交接重叠证明'), '可证重叠')
        self.assertEqual(self.decision('起落钩闭环核验'), '完工')
        self.assertEqual(self.decision('监理独立见证'), '见证')
        self.assertEqual(self.decision('当班指挥绑定'), '绑定')
        self.assertEqual(self.decision('值守空档处置'), '无空档')
        self.assertEqual(self.work_state(), 'finished')
        # 旧决定保留：缺口 -> 覆盖
        self.assertEqual(self.compact_history('指挥资质排班确认'), ['缺口', '覆盖'])
        _, h, _, w = evaluate_all([add_token_video(handoff())])
        chain = h['J1']
        self.assertEqual(chain['coverage'], 'proven')
        self.assertEqual(chain['overlap'], {'start': at('09:29'), 'end': at('09:31')})
        self.assertEqual(chain['workable'], [{'start': at('09:00'), 'end': at('10:00')}])
        hooks = w['J1']['hook_state']
        bound = {b['id']: b['commander'] for b in hooks['bindings']}
        self.assertEqual(bound, {'H1': 'Q1', 'H2': 'Q1', 'H3': 'Q2', 'H4': 'Q2'})
        self.assertEqual(hooks['sign_commander'], 'Q2')
        entities = report(self.db)['entities']
        duty = [e for e in entities if e['subsystem'] == '值守交接账']
        self.assertEqual(duty[0]['state'], 'continuous')
        self.assertTrue(audit(self.db)['ok'])

    def test_video_gap_conservative_protocol_keeps_old_hooks_tainted(self):
        gapped = add_gapped_video(handoff(id='HO2', version=2,
                                          effective_at=at('09:36')))
        self.stage(handoff(), gapped)
        # 视频缺段且无复工令：09:27–09:33 未证实值守，暂停 J1
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('值守交接链核验'), '中断')
        self.assertEqual(self.decision('值守空档处置'), '停待核')
        self.assertEqual(self.work_state(), 'suspended')
        # 保守换班协议：Q2 在 09:33 后签发复工令，可继续但旧钩序不能自动算完成
        resumed = add_gapped_video(handoff(id='HO3', version=3,
                                           effective_at=at('09:40')))
        resumed['resume_orders'] = [resume_order()]
        self.import_event(resumed)
        self.assertEqual(self.decision('指挥资质排班确认'), '附条件覆盖')
        self.assertEqual(self.decision('值守交接链核验'), '附条件')
        self.assertEqual(self.decision('值守空档处置'), '复工令接管')
        self.assertEqual(self.decision('起落钩闭环核验'), '悬停或待核')
        self.assertEqual(self.work_state(), 'resumed')
        _, h, _, w = evaluate_all([resumed])
        chain = h['J1']
        self.assertEqual(chain['coverage'], 'conditional')
        self.assertEqual(chain['pending'],
                         [{'start': at('09:27'), 'end': at('09:33')}])
        self.assertEqual(chain['workable'], [
            {'start': at('09:00'), 'end': at('09:27')},
            {'start': at('09:33'), 'end': at('10:00')}])
        gap = chain['gaps'][0]
        self.assertTrue(gap['accepted'])
        self.assertEqual(gap['accepted_by'], 'Q2')
        self.assertTrue(w['J1']['duty_tainted'])
        self.assertFalse(w['J1']['completed'])
        self.assertEqual(w['J1']['hook_state']['stages'],
                         ['hook', 'lift', 'land', 'sign'])

    def test_signer_as_current_commander_fails_independence(self):
        proven = add_token_video(handoff())
        # 签收者恰为当时指挥员 Q2（09:45 当班）：不独立
        signed_by_q2 = handoff_work()
        signed_by_q2['hook_events'][-1]['witness'] = 'Q2'
        _, _, _, w = evaluate_all([proven], work_event=signed_by_q2)
        self.assertFalse(w['J1']['hook_state']['independent_sign'])
        self.assertEqual(w['J1']['status'], 'released')
        # 同一时刻由已交班的 Q1 签收：按当时指挥人判定为独立
        signed_by_q1 = handoff_work()
        signed_by_q1['hook_events'][-1]['witness'] = 'Q1'
        _, _, _, w2 = evaluate_all([proven], work_event=signed_by_q1)
        self.assertTrue(w2['J1']['hook_state']['independent_sign'])
        self.assertEqual(w2['J1']['status'], 'finished')
        # 旧实现绑定 asset 单一 commander（Q1），职责链把 09:45 的签收绑到当班的 Q2
        _, _, a3, w3 = evaluate_all([handoff()], work_event=handoff_work())
        self.assertEqual(a3['J1']['event']['commander']['id'], 'Q1')
        self.assertEqual(w3['J1']['hook_state']['sign_commander'], 'Q2')

    def test_successor_cert_category_or_missing_cert_blocks_staff_ok(self):
        wrong_category = add_token_video(handoff(id='HO2', version=2,
                                                 effective_at=at('09:36')))
        wrong_category['people'][1]['cert']['categories'] = ['rigging']
        self.stage(handoff(), wrong_category)
        # 视频证明同场也不能让资质类别不覆盖塔机指挥的 staff_ok 成立
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('当班资质核验'), '失格')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        # 接班人缺证
        missing = add_token_video(handoff(id='HO3', version=3,
                                          effective_at=at('09:38')))
        missing['people'][1]['cert'] = None
        self.import_event(missing)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('当班资质核验'), '失格')

    def test_same_name_wrong_cert_rejected(self):
        forged = add_token_video(handoff(id='HO2', version=2,
                                         effective_at=at('09:36')))
        forged['people'][1]['cert']['holder_id'] = 'Q2-OTHER'
        self.stage(handoff(), forged)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('当班资质核验'), '失格')
        self.assertEqual(self.decision('现场风速停复工'), '停工')

    def test_cert_suspension_after_handoff_forms_new_decision(self):
        self.stage(handoff(), add_token_video(handoff(id='HO2', version=2,
                                                      effective_at=at('09:36'))))
        self.assertEqual(self.decision('指挥资质排班确认'), '覆盖')
        # 交接后才发布的证照暂停，追溯覆盖 Q1 值守段
        suspended = add_token_video(handoff(id='HO3', version=3,
                                            effective_at=at('09:50')))
        suspended['people'][0]['suspensions'] = [{
            'published_at': at('09:45'), 'start': at('09:00'),
            'end': at('09:30'), 'reason': '复审'}]
        self.import_event(suspended)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('当班资质核验'), '失格')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        self.assertEqual(self.compact_history('指挥资质排班确认'), ['缺口', '覆盖', '缺口'])

    def test_replayed_and_forged_swipes_rejected(self):
        replayed = add_token_video(handoff(id='HO2', version=2,
                                           effective_at=at('09:36')))
        clone = copy.deepcopy(replayed['swipes'][2])
        clone['id'] = 'C5'
        replayed['swipes'].append(clone)
        forged = copy.deepcopy(replayed['swipes'][0])
        forged.update(id='C6', signature='0' * 64)
        replayed['swipes'].append(forged)
        self.stage(handoff(), replayed)
        self.assertEqual(self.decision('刷卡采录验签'), '异常')
        # 重放与伪造不充当证据，但已核验刷卡仍撑起连续链
        self.assertEqual(self.decision('值守交接链核验'), '连续')
        _, h, _, _ = evaluate_all([replayed])
        reviews = {r['id']: r['status'] for r in h['J1']['swipe_reviews']}
        self.assertEqual(reviews['C5'], 'replay')
        self.assertEqual(reviews['C6'], 'bad_signature')

    def test_backfilled_swipe_forms_new_decision(self):
        late = handoff()
        late['swipes'] = [s for s in late['swipes'] if s['id'] != 'C3']
        self.stage(late)
        _, h, _, _ = evaluate_all([late])
        self.assertEqual(h['J1']['pending'],
                         [{'start': at('09:27'), 'end': at('10:00')}])
        self.assertEqual(self.decision('值守交接链核验'), '中断')
        # 设备补传：Q2 的签到延迟上传，验签通过后收缩待核区间
        backfilled = handoff(id='HO2', version=2, effective_at=at('09:56'))
        backfilled['swipes'][2]['recorded_at'] = at('09:55')
        self.import_event(backfilled)
        _, h2, _, _ = evaluate_all([backfilled])
        self.assertEqual(h2['J1']['pending'],
                         [{'start': at('09:27'), 'end': at('09:33')}])
        review = {r['id']: r for r in h2['J1']['swipe_reviews']}['C3']
        self.assertEqual(review['status'], 'verified')
        self.assertTrue(review['backfilled'])
        self.assertGreaterEqual(len(self.history('值守交接链核验')), 2)
        self.assertTrue(audit(self.db)['ok'])

    def test_temporary_absence_cannot_be_accepted_by_resume(self):
        absent = add_token_video(handoff(id='HO2', version=2,
                                         effective_at=at('09:36')))
        absent['absences'] = [{'person': 'Q1', 'start': at('09:10'),
                               'end': at('09:20'), 'reason': '临时离场'}]
        absent['resume_orders'] = [resume_order()]
        self.stage(handoff(), absent)
        # 临时离场是可证空档，不是未证实区间，复工令不能接管
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('值守交接链核验'), '中断')
        self.assertEqual(self.decision('现场风速停复工'), '停工')
        _, h, _, _ = evaluate_all([absent])
        states = {(g['start'], g['end']): g['state'] for g in h['J1']['gaps']}
        self.assertEqual(states[(at('09:10'), at('09:20'))], 'absent')

    def test_new_plan_window_reforms_coverage(self):
        self.stage(handoff(), add_token_video(handoff(id='HO2', version=2,
                                                      effective_at=at('09:36'))))
        self.assertEqual(self.decision('指挥资质排班确认'), '覆盖')
        # 新版方案改窗到 09:00–11:20，超出名册与可证值守
        altered = copy.deepcopy(sample('plan-ok.json'))
        altered.update(id='P2', version=2, signed_version=2,
                       window_end=at('11:20'))
        self.import_event(altered)
        self.assertEqual(self.decision('指挥资质排班确认'), '缺口')
        self.assertEqual(self.decision('值守交接链核验'), '中断')
        self.assertEqual(self.compact_history('指挥资质排班确认'), ['缺口', '覆盖', '缺口'])
        plan_events = [sample('plan-ok.json'), altered]
        p = plans.evaluate(plan_events)
        chain = handoff_capture.evaluate(
            [add_token_video(handoff())], p)['J1']
        self.assertEqual(chain['coverage'], 'broken')
        self.assertIn({'start': at('10:57'), 'end': at('11:00')}, chain['pending'])
        unrostered = [g for g in chain['gaps'] if g['state'] == 'unrostered']
        self.assertEqual(unrostered[0]['start'], at('11:00'))

    def test_unproven_gap_while_hooked_suspends_and_keeps_sequence(self):
        self.stage(handoff())
        self.assertEqual(self.work_state(), 'suspended')
        _, _, _, w = evaluate_all([handoff()])
        hooks = w['J1']['hook_state']
        self.assertEqual(hooks['stages'], ['hook', 'lift', 'land', 'sign'])
        self.assertFalse(w['J1']['completed'])
        self.assertTrue(w['J1']['duty_blocked'])
        # 离地未落钩时出现无法证明的空档：应急落地
        airborne = handoff_work(id='WH2', version=2)
        airborne['hook_events'] = airborne['hook_events'][:2]
        self.import_event(airborne)
        self.assertEqual(self.decision('悬吊物应急落地'), '应急落地:LS1')

    def test_capture_import_validation_and_records(self):
        broken = handoff()
        del broken['readers']
        path = Path(self.tmp.name) / 'broken.json'
        path.write_text(json.dumps([broken], ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'readers'):
            import_file(self.db, 'handoff', path)
        self.stage(handoff())
        records = report(self.db)['business_records']
        types = {r['entity_type'] for r in records if r['subsystem'] == '值守交接账'}
        self.assertIn('刷卡采录', types)
        self.assertIn('值守班次', types)
        self.assertTrue(audit(self.db)['ok'])


if __name__ == '__main__':
    unittest.main()
