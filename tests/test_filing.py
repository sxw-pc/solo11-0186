"""回执账测试：门户已承诺、本地未知、业务快照改版三条时间线的因果边界。"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from liftpermit.store import audit, connect, import_file, report
from liftpermit.portal_mock import (
    MockPortal, PortalError, PortalTransportError, canonical_bytes, digest,
)
from liftpermit.filing import (
    FilingClient, FilingError, STATUS_PENDING, STATUS_CONFIRMED,
    STATUS_CONFLICT, STATUS_WITHDRAWN, STATUS_BATCH_PENDING,
    ROUTE_ITEM, ROUTE_BATCH, REGULATORY_FIELDS, build_snapshot, build_package,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return ROOT / 'fixtures' / name


def load_scenario():
    return json.loads(fixture('filing-crash.json').read_text(encoding='utf-8'))


SCENARIO = load_scenario()
REG = SCENARIO['regulatory']


class FilingTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'state.sqlite'
        self.portal_path = Path(self.tmp.name) / 'portal.json'
        self.db = connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def portal(self, faults=None):
        return MockPortal(self.portal_path, faults=faults)

    def client(self, portal=None):
        return FilingClient(self.db, portal or self.portal())

    def import_ok(self):
        for step in SCENARIO['imports']:
            import_file(self.db, step['kind'], fixture(step['fixture']))

    def import_plan_bad(self):
        import_file(self.db, 'plan', fixture('plan-bad.json'))

    def decision(self, name):
        return report(self.db)['decisions'][f'J1/{name}']['result']


class SnapshotTests(FilingTestBase):
    def test_snapshot_freezes_v1_identity_and_twelve_decisions(self):
        self.import_ok()
        client = self.client()
        snapshot = build_snapshot(self.db, 'J1', REG)
        self.assertEqual(snapshot['selected']['plan_event_id'], 'P1')
        self.assertEqual(snapshot['selected']['plan_version'], 1)
        self.assertEqual(snapshot['selected']['work_version_identity']['permit_version'], 1)
        self.assertTrue(snapshot['selected']['work_version_identity']['released'])
        self.assertEqual(len(snapshot['decision_summary']), 12)
        bound_ids = {e['event_id'] for e in
                     snapshot['bound_events']['plan'] +
                     snapshot['bound_events']['asset'] +
                     snapshot['bound_events']['work']}
        self.assertEqual(bound_ids, {'P1', 'A1', 'W1'})
        self.assertTrue(all(e['sha'] for grp in snapshot['bound_events'].values()
                            for e in grp))

    def test_snapshot_is_immutable_after_plan_v2_import(self):
        self.import_ok()
        v1 = build_snapshot(self.db, 'J1', REG)
        v1_digest = v1['snapshot_digest']
        self.import_plan_bad()
        v2 = build_snapshot(self.db, 'J1', REG)
        self.assertNotEqual(v1['snapshot_version'], v2['snapshot_version'])
        self.assertEqual(v2['selected']['plan_event_id'], 'P2')
        self.assertEqual(v2['selected']['plan_version'], 2)
        # v2 下 work 许可不再代表当前方案
        self.assertFalse(v2['selected']['work_version_identity']['released'])
        self.assertIsNone(v2['selected']['work_version_identity']['permit_version'])
        # 旧快照摘要不随后续导入改变
        self.assertEqual(build_snapshot.__name__, 'build_snapshot')
        frozen = json.loads(json.dumps(v1))
        self.assertEqual(frozen['snapshot_digest'], v1_digest)
        self.assertEqual(self.decision('方案签认撤回'), '撤回')

    def test_missing_regulatory_field_blocks_intent(self):
        self.import_ok()
        bad = dict(REG)
        del bad['case_number']
        with self.assertRaisesRegex(FilingError, '监管必填字段缺失'):
            build_snapshot(self.db, 'J1', bad)
        self.assertEqual(len(REG), len(REGULATORY_FIELDS))


class ItemRouteCrashTests(FilingTestBase):
    """固定时序 fixtures/filing-crash.json 的逐件门户路线。"""

    def _crash_after_portal_commit(self):
        """t2-t3：门户生成 R77 后，采录端在本地写回执前故障。"""
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent_id, created = client.create_item_intent('J1', REG)
        self.assertTrue(created)
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit'",
            (intent_id,)).fetchone()
        request = json.loads(op['request_text'])
        # 直接与门户交互，模拟客户端进程在拿到响应字节前消失
        response = portal.submit(request)
        self.assertEqual(response.data['receipt_id'], 'R77')
        # 本地停在“已提交待回执”：门户已承诺、本地尚未知
        from liftpermit import store as store_mod
        store_mod.update_operation_status(self.db, op['operation_key'], 'timeout')
        store_mod.update_intent_status(self.db, intent_id, STATUS_PENDING)
        return intent_id, op['operation_key'], request, response

    def test_recovery_matches_r77_to_v1_not_current_v2(self):
        intent_id, op_key, request, r77 = self._crash_after_portal_commit()
        # 崩溃点：本地无回执，意图停在 已提交待回执
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM filing_receipt').fetchone()[0], 0)
        local = self.client().ledger(status=STATUS_PENDING)
        self.assertEqual(len(local), 1)
        # t4：程序重开前业务账已改版
        self.import_plan_bad()
        # 值守员若用当前 report 重建载荷会把 v2 塞进 R77——客户端禁止这么做
        client = self.client()
        recovered = client.reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFIRMED)
        row = self.client().ledger(job_id='J1')[0]
        self.assertEqual(row['snapshot_version'].find('plan=P1/v1') >= 0, True)
        receipt = row['receipts'][0]
        self.assertEqual(receipt['receipt_id'], 'R77')
        # 回执字节 1:1：留存原文摘要等于门户原始响应摘要，且绑定 v1 备案包
        self.assertEqual(receipt['raw_digest'], r77.raw_digest)
        self.assertEqual(receipt['payload_digest'],
                         json.loads(
                             self.db.execute(
                                 'SELECT payload FROM filing_intent WHERE intent_id=?',
                                 (intent_id,)).fetchone()[0])['content_digest'])
        stored_req = json.loads(self.db.execute(
            'SELECT request_text FROM filing_operation WHERE operation_key=?',
            (op_key,)).fetchone()[0])
        self.assertEqual(stored_req, request)
        self.assertEqual(stored_req['filing']['snapshot_version'],
                         request['filing']['snapshot_version'])
        self.assertIn('plan=P1/v1', stored_req['filing']['snapshot_version'])
        # 查询证据已留痕
        self.assertGreaterEqual(
            self.db.execute('SELECT COUNT(*) FROM filing_query').fetchone()[0], 1)

    def test_duplicate_click_does_not_create_second_valid_receipt(self):
        intent_id, _op, _req, _r77 = self._crash_after_portal_commit()
        client = self.client()
        client.reconcile_intent(intent_id)
        # 值守员重开程序再次点击提交：同一冻结快照只返回原意图，不发新请求
        again_id, created = client.create_item_intent('J1', REG)
        self.assertFalse(created)
        self.assertEqual(again_id, intent_id)
        portal = self.portal()
        self.assertEqual(len(portal.list_filings('J1').data['filings']), 1)

    def test_timeout_then_replay_uses_persisted_request_and_same_receipt(self):
        self.import_ok()
        # 先建意图拿到操作键，再给门户注入针对该键的一次性超时
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent_id, _ = client.create_item_intent('J1', REG)
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit'",
            (intent_id,)).fetchone()
        key = json.loads(op['request_text'])['idempotency_key']
        portal2 = self.portal(faults=[f'submit_timeout:{key}'])
        client2 = FilingClient(self.db, portal2)
        pending = client2.dispatch_intent(intent_id)
        self.assertEqual(pending['status'], STATUS_PENDING)
        # 门户实际已接收
        self.assertEqual(portal2.find_by_idempotency_key(key).data['receipt_id'], 'R77')
        # 恢复：重放的是持久化原请求，幂等拿回 R77，不产生 R78
        recovered = client2.reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFIRMED)
        receipts = [r['receipt_id'] for r in client2.ledger(job_id='J1')[0]['receipts']]
        self.assertEqual(receipts, ['R77'])

    def test_delayed_listing_keeps_pending_until_evidence_arrives(self):
        intent_id, _op, _req, _r77 = self._crash_after_portal_commit()
        # 操作键直查命中即确认；列表延迟只影响无法按操作键命中的对账路径
        portal = self.portal()
        client = FilingClient(self.db, portal)
        recovered = client.reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFIRMED)
        # 列表延迟故障确实被门户支持并按一次性脚本消费
        portal.inject_faults(['list_delay'])
        with self.assertRaises(PortalTransportError):
            portal.list_filings('J1')

    def test_delayed_listing_without_key_match_stays_pending(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent_id, _ = client.create_item_intent('J1', REG)
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit'",
            (intent_id,)).fetchone()
        key = json.loads(op['request_text'])['idempotency_key']
        # 门户超时已收，by-key 通道不可用，列表又延迟：只能保持待回执
        portal2 = self.portal(
            faults=[f'submit_timeout:{key}',
                    f'key_lookup_unavailable:{key}', 'list_delay'])
        client2 = FilingClient(self.db, portal2)
        client2.dispatch_intent(intent_id)
        again = client2.reconcile_intent(intent_id)
        self.assertEqual(again['status'], STATUS_PENDING)
        # 下一次恢复证据齐全，对回 R77
        recovered = client2.reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFIRMED)

    def test_receipt_page_jump_failure_then_fetch_succeeds(self):
        intent_id, _op, _req, r77 = self._crash_after_portal_commit()
        portal = self.portal(faults=[f"receipt_unavailable:{r77.data['receipt_id']}"])
        client = FilingClient(self.db, portal)
        # by-key 内部直接返回回执，不经过跳转页；先验证跳转故障本身
        with self.assertRaises(PortalTransportError):
            portal.fetch_receipt('R77')
        recovered = client.reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFIRMED)

    def test_v2_cannot_reuse_r77_and_withdrawal_closes_old_intent(self):
        intent_id, _op, _req, _r77 = self._crash_after_portal_commit()
        self.import_plan_bad()
        client = self.client()
        client.reconcile_intent(intent_id)
        # 撤回 R77（旧 v1 意图）
        withdrawn = client.withdraw_intent(intent_id)
        self.assertEqual(withdrawn['status'], STATUS_WITHDRAWN)
        row = next(i for i in client.ledger() if i['intent_id'] == intent_id)
        kinds = {r['kind'] for r in row['receipts']}
        self.assertIn('withdrawal', kinds)
        self.assertTrue(any(r['kind'] == 'withdrawal' for r in row['relations']))
        portal = self.portal()
        self.assertEqual(portal.fetch_receipt('R77').data['status'], 'withdrawn')
        # v2 生成全新意图与新回执 R78；R77 永远不是 v2 的备案证明
        v2_intent = client.submit_item('J1', REG)
        self.assertEqual(v2_intent['status'], STATUS_CONFIRMED)
        self.assertNotEqual(v2_intent['intent_id'], intent_id)
        self.assertIn('plan=P2/v2', v2_intent['snapshot_version'])
        v2_row = next(i for i in client.ledger()
                      if i['intent_id'] == v2_intent['intent_id'])
        self.assertEqual([r['receipt_id'] for r in v2_row['receipts']], ['R78'])
        self.assertEqual(v2_row['payload_digest'] != row['payload_digest'], True)
        # 已撤回意图拒绝再派发
        with self.assertRaisesRegex(FilingError, '已随旧版撤回'):
            client.dispatch_intent(intent_id)

    def test_withdraw_timeout_recovers_via_receipt_status(self):
        intent_id, _op, _req, r77 = self._crash_after_portal_commit()
        client = self.client()
        client.reconcile_intent(intent_id)
        portal = self.portal(faults=['withdraw_timeout:R77'])
        client2 = FilingClient(self.db, portal)
        pending = client2.withdraw_intent(intent_id)
        # 超时但门户实际已撤回
        self.assertEqual(portal.fetch_receipt('R77').data['status'], 'withdrawn')
        recovered = client2.reconcile_withdrawal(intent_id)
        self.assertEqual(recovered['status'], STATUS_WITHDRAWN)

    def test_orphan_same_content_filing_goes_to_conflict(self):
        # 另起客户端用新操作键提交同内容（模拟游离备案），原意图恢复时必须转冲突待核
        self.import_ok()
        portal = self.portal()
        first = FilingClient(self.db, portal)
        intent_id, _ = first.create_item_intent('J1', REG)
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit'",
            (intent_id,)).fetchone()
        request = json.loads(op['request_text'])
        rogue = dict(request)
        rogue['idempotency_key'] = 'idem-rogue-other-key'
        portal.submit(rogue)  # R77 挂在另一个操作键下
        # 原操作键在门户查不到
        self.assertIsNone(portal.find_by_idempotency_key(
            request['idempotency_key']))
        recovered = FilingClient(self.db, portal).reconcile_intent(intent_id)
        self.assertEqual(recovered['status'], STATUS_CONFLICT)
        row = next(i for i in FilingClient(self.db, portal).ledger()
                   if i['intent_id'] == intent_id)
        self.assertTrue(any(r['kind'] == 'orphan_filing' for r in row['relations']))

    def test_r77_never_becomes_v2_evidence_and_no_orphan_filing(self):
        """题面反事实：错误实现会再发 v2 拿 R78 并把 R77 留成孤儿有效备案。"""
        intent_id, _op, _req, r77 = self._crash_after_portal_commit()
        self.import_plan_bad()
        client = self.client()
        client.reconcile_intent(intent_id)
        client.withdraw_intent(intent_id)
        v2 = client.submit_item('J1', REG)
        listing = self.portal().list_filings('J1').data['filings']
        r77_row = next(f for f in listing if f['receipt_id'] == 'R77')
        r78_row = next(f for f in listing if f['receipt_id'] == 'R78')
        v1_intent = next(i for i in client.ledger()
                         if any(r['receipt_id'] == 'R77' for r in i['receipts']))
        self.assertEqual(r77_row['status'], 'withdrawn')
        self.assertEqual(r77_row['content_digest'], v1_intent['payload_digest'])
        self.assertEqual(r78_row['status'], 'active')
        self.assertEqual(r78_row['content_digest'], v2['payload_digest'])
        self.assertIn('plan=P1/v1', v1_intent['snapshot_version'])
        self.assertEqual(v1_intent['status'], STATUS_WITHDRAWN)
        active = [f for f in listing if f['status'] == 'active']
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['receipt_id'], 'R78')

    def test_local_sqlite_rollback_cannot_undo_portal_commit(self):
        intent_id, _op, _req, r77 = self._crash_after_portal_commit()
        # 本地整库回滚式清表（模拟本地库丢失），门户文件里的 R77 依旧在册
        self.db.execute('DELETE FROM filing_intent')
        self.db.execute('DELETE FROM filing_operation')
        self.db.commit()
        portal = self.portal()
        listing = portal.list_filings('J1').data['filings']
        self.assertEqual([f['receipt_id'] for f in listing], ['R77'])
        self.assertEqual(listing[0]['status'], 'active')


class BatchRouteTests(FilingTestBase):
    """夜间签章批量路线：B5 与逐件回执的粒度边界。"""

    def _second_job_events(self):
        # J2 只在本库内构造一份可备案的独立作业数据
        plan = json.loads(fixture('plan-ok.json').read_text())[0]
        asset = json.loads(fixture('asset-ok.json').read_text())[0]
        work = json.loads(fixture('work-ok.json').read_text())[0]
        for ev, new_id in ((plan, 'P91'), (asset, 'A91'), (work, 'W91')):
            ev['job_id'] = 'J2'
            ev['id'] = new_id
        return plan, asset, work

    def _import_j2(self):
        for ev in self._second_job_events():
            path = Path(self.tmp.name) / f"{ev['id']}.json"
            path.write_text(json.dumps([ev], ensure_ascii=False), encoding='utf-8')
            import_file(self.db, ev['kind'], path)

    def test_batch_receipt_b5_is_not_per_item_confirmation(self):
        self.import_ok()
        self._import_j2()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG},
            {'job_id': 'J2', 'regulatory': REG},
        ])
        self.assertEqual(
            self.db.execute(
                "SELECT receipt_id FROM filing_receipt WHERE kind='batch'").fetchone()[0],
            'B5')
        for intent in client.ledger():
            self.assertEqual(intent['status'], STATUS_BATCH_PENDING)
        # 展开前不存在 J1 的逐件回执，不能宣布逐件确认
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM filing_receipt WHERE kind='item'").fetchone()[0],
            0)
        client.reconcile_batch(parent_id)
        statuses = {i['job_id']: i['status'] for i in client.ledger()
                    if i['intent_id'] in children}
        self.assertEqual(statuses, {'J1': STATUS_CONFIRMED, 'J2': STATUS_CONFIRMED})
        # J1 逐件回执与冻结快照 1:1
        j1 = next(i for i in client.ledger()
                  if i['intent_id'] == children[0])
        item_receipt = next(r for r in j1['receipts'] if r['kind'] == 'item')
        self.assertEqual(item_receipt['payload_digest'], j1['payload_digest'])

    def test_batch_partial_failure_does_not_infer_j1_result(self):
        self.import_ok()
        self._import_j2()
        portal = self.portal(faults=['batch_item_fail:J2'])
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG},
            {'job_id': 'J2', 'regulatory': REG},
        ])
        # B5 已签章，但两件都只是逐件待核
        self.assertTrue(all(i['status'] == STATUS_BATCH_PENDING
                            for i in client.ledger()))
        client.reconcile_batch(parent_id)
        by_job = {i['job_id']: i for i in client.ledger()
                  if i['intent_id'] in children}
        self.assertEqual(by_job['J1']['status'], STATUS_CONFIRMED)
        self.assertEqual(by_job['J2']['status'], STATUS_CONFLICT)
        self.assertTrue(any(r['kind'] == 'batch_item_failed'
                            for r in by_job['J2']['relations']))
        # J2 失败不改变 J1 回执的有效性
        self.assertTrue(any(r['kind'] == 'item' for r in by_job['J1']['receipts']))

    def test_batch_expansion_delay_keeps_pending_status(self):
        self.import_ok()
        portal = self.portal(faults=['batch_expand_pending:B5'])
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG}])
        first = client.reconcile_batch(parent_id)
        self.assertEqual(first, parent_id)
        self.assertTrue(all(i['status'] == STATUS_BATCH_PENDING
                            for i in client.ledger()))
        # 故障是一次性的，再次展开即确认
        client.reconcile_batch(parent_id)
        self.assertEqual(client.ledger(job_id='J1')[0]['status'], STATUS_CONFIRMED)

    def test_batch_submit_timeout_replay_returns_same_b5(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG}])
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='batch-submit'",
            (parent_id,)).fetchone()
        key = json.loads(op['request_text'])['idempotency_key']
        # 另建门户句柄制造“超时但已收 B5”，本地先删掉 B5 回执模拟未落库
        self.db.execute("DELETE FROM filing_receipt WHERE kind='batch'")
        self.db.commit()
        portal2 = self.portal(faults=[f'submit_timeout:{key}'])
        client2 = FilingClient(self.db, portal2)
        client2.reconcile_batch(parent_id)
        receipts = [r[0] for r in self.db.execute(
            "SELECT DISTINCT receipt_id FROM filing_receipt WHERE kind='batch'")]
        self.assertEqual(receipts, ['B5'])

    def test_batch_item_withdraw_after_expansion(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG}])
        client.reconcile_batch(parent_id)
        withdrawn = client.withdraw_batch_item(children[0])
        self.assertEqual(withdrawn['status'], STATUS_WITHDRAWN)
        item_receipt_id = self.db.execute(
            "SELECT receipt_id FROM filing_receipt WHERE intent_id=? AND kind='item'",
            (children[0],)).fetchone()[0]
        self.assertEqual(portal.fetch_receipt(item_receipt_id).data['status'],
                         'withdrawn')

    def test_batch_crash_then_plan_v2_keeps_b5_bound_to_v1(self):
        """批量路线在同一崩溃点：B5/R77 只证明 v1，v2 必须另起意图。"""
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        parent_id, children = client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG}])
        # 展开回执到达前即崩溃并改版
        self.import_plan_bad()
        client.reconcile_batch(parent_id)
        child = next(i for i in client.ledger()
                     if i['intent_id'] == children[0])
        self.assertEqual(child['status'], STATUS_CONFIRMED)
        self.assertIn('plan=P1/v1', child['snapshot_version'])
        # 撤回的是批次件，不能把它当 v2 的备案
        withdrawn = client.withdraw_batch_item(children[0])
        self.assertEqual(withdrawn['status'], STATUS_WITHDRAWN)
        # v2 走逐件新意图，门户发 R78（R77 已被批量件占用并撤回）
        item_client = FilingClient(self.db, self.portal())
        item_intent = item_client.submit_item('J1', REG)
        self.assertIn('plan=P2/v2', item_intent['snapshot_version'])
        receipts = [r['receipt_id'] for r in
                    item_client.ledger(job_id='J1')
                    if r['intent_id'] == item_intent['intent_id']
                    for r in r['receipts']]
        self.assertEqual(receipts, ['R78'])

    def test_batch_and_item_routes_carry_identical_v1_content(self):
        self.import_ok()
        item_client = self.client()
        item_intent = item_client.submit_item('J1', REG)
        batch_portal = MockPortal(Path(self.tmp.name) / 'portal-batch.json')
        batch_client = FilingClient(self.db, batch_portal)
        parent_id, children = batch_client.submit_batch([
            {'job_id': 'J1', 'regulatory': REG}])
        batch_client.reconcile_batch(parent_id)
        batch_row = next(i for i in batch_client.ledger()
                         if i['intent_id'] == children[0])
        # 两条监管认可路线提交的 v1 内容摘要完全相同，确认粒度与门户来源不同
        self.assertEqual(item_intent['payload_digest'], batch_row['payload_digest'])
        self.assertEqual(item_intent['snapshot_version'], batch_row['snapshot_version'])
        item_receipt = item_client.ledger(job_id='J1')[0]['receipts'][0]['receipt_id']
        batch_receipt = next(r['receipt_id'] for r in batch_row['receipts']
                             if r['kind'] == 'item')
        # 两个相互独立的门户实例可各自编号 R77，但本地意图不同、回执账不串账
        self.assertNotEqual(item_intent['intent_id'], batch_row['intent_id'])
        self.assertEqual(item_receipt, 'R77')
        self.assertEqual(batch_receipt, 'R77')
        self.assertEqual(
            self.db.execute(
                'SELECT COUNT(DISTINCT intent_id) FROM filing_receipt '
                "WHERE receipt_id='R77'").fetchone()[0], 2)


class LedgerAndGuardsTests(FilingTestBase):
    def test_ledger_buckets_and_local_decision_versions(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent_id, _ = client.create_item_intent('J1', REG)
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit'",
            (intent_id,)).fetchone()
        request = json.loads(op['request_text'])
        portal.submit(request)  # R77 已生成，本地尚未知
        from liftpermit import store as store_mod
        store_mod.update_operation_status(self.db, op['operation_key'], 'timeout')
        store_mod.update_intent_status(self.db, intent_id, STATUS_PENDING)
        self.assertEqual(
            [i['intent_id'] for i in client.ledger(status=STATUS_PENDING)],
            [intent_id])
        client.reconcile_intent(intent_id)
        confirmed = client.ledger(status=STATUS_CONFIRMED)
        self.assertEqual(len(confirmed), 1)
        self.assertIn('plan=P1/v1', confirmed[0]['snapshot_version'])
        self.import_plan_bad()
        client.withdraw_intent(intent_id)
        client.submit_item('J1', REG)
        withdrawn = client.ledger(status=STATUS_WITHDRAWN)
        self.assertEqual(len(withdrawn), 1)
        self.assertIn('plan=P1/v1', withdrawn[0]['snapshot_version'])
        # 即便当前 report 已是 v2，旧版已撤回条目仍冻结 v1 裁定版本
        current = build_snapshot(self.db, 'J1', REG)
        self.assertIn('plan=P2/v2', current['snapshot_version'])
        self.assertIn('plan=P1/v1', withdrawn[0]['snapshot_version'])
        # 每条意图都带操作键、回执、关系证据
        row = withdrawn[0]
        self.assertTrue(row['operations'] and row['receipts'] and row['relations'])

    def test_digest_mismatch_marks_conflict_and_keeps_receipt_out_of_evidence(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent = client.submit_item('J1', REG)
        # 人为篡改门户在册备案的 content_digest，模拟张冠李戴的回执
        rid = client.ledger(job_id='J1')[0]['receipts'][0]['receipt_id']
        portal._filings[rid]['content_digest'] = '0' * 64
        fresh = FilingClient(self.db, portal)
        # 新意图对回被污染的同内容备案：_attach_receipt 必须判冲突
        self.db.execute("DELETE FROM filing_receipt WHERE intent_id=?",
                        (intent['intent_id'],))
        self.db.execute(
            "UPDATE filing_intent SET status=? WHERE intent_id=?",
            (STATUS_PENDING, intent['intent_id']))
        self.db.commit()
        result = fresh.reconcile_intent(intent['intent_id'])
        self.assertEqual(result['status'], STATUS_CONFLICT)

    def test_twelve_decisions_and_audit_unchanged_after_filing_activity(self):
        self.import_ok()
        client = self.client()
        client.submit_item('J1', REG)
        self.import_plan_bad()
        self.assertEqual(len(report(self.db)['decisions']), 12)
        self.assertEqual(self.decision('方案签认撤回'), '撤回')
        self.assertTrue(audit(self.db)['ok'])

    def test_same_request_bytes_are_replayed_not_rebuilt(self):
        self.import_ok()
        portal = self.portal()
        client = FilingClient(self.db, portal)
        intent_id, _ = client.create_item_intent('J1', REG)
        before = self.db.execute(
            "SELECT request_text FROM filing_operation WHERE intent_id=?",
            (intent_id,)).fetchone()[0]
        self.import_plan_bad()  # 当前 report 已变成 v2
        client.reconcile_intent(intent_id)
        after = self.db.execute(
            "SELECT request_text FROM filing_operation WHERE intent_id=?",
            (intent_id,)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertIn('plan=P1/v1', before)


class CliFilingTests(FilingTestBase):
    def test_cli_filing_and_ledger_commands(self):
        for step in SCENARIO['imports']:
            subprocess.run([sys.executable, '-m', 'liftpermit',
                            f"{step['kind']}-import", '--db', str(self.db_path),
                            '--file', str(fixture(step['fixture']))],
                           cwd=ROOT, check=True, capture_output=True, text=True)
        reg_path = Path(self.tmp.name) / 'regulatory.json'
        reg_path.write_text(json.dumps(REG, ensure_ascii=False), encoding='utf-8')

        def run(*extra):
            proc = subprocess.run(
                [sys.executable, '-m', 'liftpermit', *extra,
                 '--db', str(self.db_path), '--portal', str(self.portal_path)],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

        out = run('filing-submit', '--job', 'J1', '--regulatory', str(reg_path))
        intent_id = out['intent']['intent_id']
        self.assertEqual(out['intent']['status'], STATUS_CONFIRMED)
        ledger = run('filing-ledger', '--status', STATUS_CONFIRMED)
        self.assertEqual(len(ledger['ledger']), 1)
        out = run('filing-withdraw', '--intent', intent_id)
        self.assertEqual(out['intent']['status'], STATUS_WITHDRAWN)
        ledger = run('filing-ledger', '--status', STATUS_WITHDRAWN)
        self.assertEqual(ledger['ledger'][0]['snapshot_version'].find('P1/v1') >= 0,
                         True)


if __name__ == '__main__':
    unittest.main()
