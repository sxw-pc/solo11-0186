"""备案 RPA 客户端：本地三账裁定 → 监管门户回执账。

三条独立时间线必须分开对待：
1. 本地业务账（plan/asset/work 事件）随时可能导入改版；
2. 门户一旦签章即承诺，本地 SQLite 事务无法回滚门户；
3. 采录程序可能在“门户已承诺、本地尚未知”的窗口崩溃。

因此本模块的铁律是：
- 备案包在任何门户调用之前整体持久化（提交意图 + 操作键 + 原请求字节）；
- 重放只重发已持久化的原请求，绝不用当前 report 重建旧意图的载荷；
- 回执字节原样落库，并按 content_digest 与冻结的备案包一一核对，不一致即“冲突待核”；
- 方案改版后旧意图只能走撤回，新快照生成新意图，旧回执永远不能充当新备案证明。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from . import rules
from . import store as store_mod
from .portal_mock import (
    MockPortal, PortalError, PortalTransportError, canonical_bytes, digest,
)

ROUTE_ITEM = 'item'
ROUTE_BATCH = 'batch-seal'

# 监管必填字段：缺任一项不允许生成提交意图
REGULATORY_FIELDS = (
    'project_name', 'contractor_name', 'site_code', 'crane_id',
    'commander_id', 'monitor_id', 'window_start', 'window_end',
    'filing_clerk', 'contact_phone', 'case_number', 'filing_purpose',
)

STATUS_DRAFT = '待提交'
STATUS_PENDING = '已提交待回执'
STATUS_BATCH_PENDING = '批次已收逐件待核'
STATUS_CONFIRMED = '已确认'
STATUS_CONFLICT = '冲突待核'
STATUS_WITHDRAWN = '旧版已撤回'


class FilingError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_events(db):
    return [json.loads(row[0]) for row in
            db.execute('SELECT body FROM events ORDER BY seq')]


def build_snapshot(db, job_id, regulatory):
    """冻结某一时点的三账裁定快照。返回后即不可变，后续导入不影响其内容。"""
    missing = [f for f in REGULATORY_FIELDS if not regulatory.get(f)]
    if missing:
        raise FilingError(f'监管必填字段缺失: {", ".join(missing)}')
    events = _load_events(db)
    if not events:
        raise FilingError('本地无任何导入事件，无法形成备案快照')
    decisions, _states = rules.calculate(events)
    plan_events = [e for e in events if e['kind'] == 'plan' and e['job_id'] == job_id]
    asset_events = [e for e in events if e['kind'] == 'asset' and e['job_id'] == job_id]
    work_events = [e for e in events if e['kind'] == 'work' and e['job_id'] == job_id]
    if not plan_events or not work_events:
        raise FilingError(f'作业 {job_id} 缺少 plan/work 事件，不能备案')
    plans = rules.plans.evaluate(plan_events)
    assets = rules.resources.evaluate(asset_events, plans)
    works = rules.work.evaluate(work_events, plans, assets)
    if job_id not in works:
        raise FilingError(f'作业 {job_id} 未形成现场许可裁定')
    plan = plans.get(job_id)
    asset = assets.get(job_id)
    work = works[job_id]
    sha_map = {row[0]: row[1] for row in db.execute('SELECT event_id, sha FROM events')}

    def bind(evs):
        return [{'event_id': e['id'], 'kind': e['kind'], 'version': int(e['version']),
                 'effective_at': e.get('effective_at'), 'sha': sha_map.get(e['id'])}
                for e in evs]

    selected = {
        'plan_event_id': plan['event']['id'] if plan else None,
        'plan_version': int(plan['event']['version']) if plan else None,
        'asset_event_id': asset['event']['id'] if asset else None,
        'asset_version': int(asset['event']['version']) if asset else None,
        'work_event_id': work['event']['id'],
        'work_version': int(work['event']['version']),
        'plan_version_identity': plan['version_identity'] if plan else None,
        'work_version_identity': work['version_identity'],
        'permit_version': work['permit_version'],
        'released': work['released'],
    }
    identity = ':'.join([
        job_id,
        f"plan={selected['plan_event_id']}/v{selected['plan_version']}",
        f"asset={selected['asset_event_id']}/v{selected['asset_version']}",
        f"work={selected['work_event_id']}/v{selected['work_version']}",
    ])
    # 十二项裁定摘要（名称/结果/理由/证据事件）
    decision_summary = [
        {'name': name, **decisions[name]}
        for name in sorted(decisions) if name.startswith(f'{job_id}/')
    ]
    snapshot = {
        'snapshot_type': 'lift_completion_snapshot',
        'job_id': job_id,
        'captured_at': _now(),
        'snapshot_version': identity,
        'bound_events': {
            'plan': bind(plan_events),
            'asset': bind(asset_events),
            'work': bind(work_events),
        },
        'selected': selected,
        'work_status': {
            'status': work['status'], 'active': work['active'],
            'completed': work['completed'],
            'permit_version': work['permit_version'], 'released': work['released'],
        },
        'decision_summary': decision_summary,
        'regulatory': {f: regulatory[f] for f in REGULATORY_FIELDS},
    }
    # 摘要只覆盖业务内容：同一时点裁定无论何时重算，digest 必须稳定一致
    digest_input = {k: v for k, v in snapshot.items() if k != 'captured_at'}
    snapshot['snapshot_digest'] = digest(digest_input)
    return snapshot


def build_package(snapshot):
    """备案包：显式绑定事件 ID/版本、十二项裁定摘要与监管必填字段。"""
    # 备案包是可复算的业务凭据：嵌入快照不含墙钟时间，同一裁定重算得到同一摘要
    embedded = {k: v for k, v in snapshot.items() if k != 'captured_at'}
    package = {
        'package_type': 'lift_completion_filing',
        'package_version': 1,
        'job_id': snapshot['job_id'],
        'snapshot_version': snapshot['snapshot_version'],
        'snapshot_digest': snapshot['snapshot_digest'],
        'bound_events': snapshot['bound_events'],
        'selected': snapshot['selected'],
        'decision_summary': snapshot['decision_summary'],
        'regulatory': snapshot['regulatory'],
        'snapshot': embedded,
    }
    package['content_digest'] = digest(package)
    return package


class FilingClient:
    """持久化提交意图、门户操作键、原请求、回执原文、查询证据与撤回关系。"""

    def __init__(self, db, portal: MockPortal):
        self.db = db
        self.portal = portal

    # ---------------------------------------------------------- 意图与请求
    def _persist_intent(self, job_id, route, package, *, parent=None,
                        batch_id=None, item_key=None):
        intent_id = _new_id('intent')
        store_mod.insert_intent(self.db, {
            'intent_id': intent_id, 'job_id': job_id, 'route': route,
            'status': STATUS_DRAFT, 'parent_intent_id': parent,
            'snapshot_version': package['snapshot_version'],
            'payload': package, 'payload_digest': package['content_digest'],
            'batch_id': batch_id, 'item_key': item_key,
        })
        return intent_id

    def _persist_request(self, intent_id, kind, request, endpoint):
        operation_key = _new_id('op')
        raw = canonical_bytes(request)
        store_mod.insert_operation(self.db, {
            'operation_key': operation_key, 'intent_id': intent_id,
            'kind': kind, 'status': 'persisted',
            'request_text': raw.decode('utf-8'),
            'request_digest': _sha(raw),
            'portal_endpoint': endpoint,
        })
        return operation_key

    def _stored_request(self, operation_key):
        op = store_mod.get_operation(self.db, operation_key)
        if not op:
            raise FilingError(f'本地无操作键 {operation_key}')
        return json.loads(op['request_text'])

    def _record_query(self, operation_key, endpoint, query_text, raw_text):
        store_mod.insert_query(self.db, {
            'query_id': _new_id('query'), 'operation_key': operation_key,
            'endpoint': endpoint, 'query_text': str(query_text),
            'raw_text': str(raw_text),
        })

    def _attach_receipt(self, intent_id, operation_key, response, *,
                        kind='item', batch_id=None):
        # 1:1 核对：门户回执的 content_digest 必须等于本地冻结备案包摘要
        intent = store_mod.get_intent(self.db, intent_id)
        if response.data.get('content_digest') != intent['payload_digest']:
            store_mod.update_intent_status(self.db, intent_id, STATUS_CONFLICT)
            store_mod.update_operation_status(self.db, operation_key, 'conflict')
            store_mod.insert_relation(self.db, {
                'relation_id': _new_id('rel'), 'intent_id': intent_id,
                'kind': 'digest_mismatch',
                'receipt_id': response.data.get('receipt_id'),
                'note': ('门户回执 content_digest 与本地冻结备案包不一致，'
                         '禁止把该回执当作本意图的备案证明'),
            })
            return False
        store_mod.insert_receipt(self.db, {
            'receipt_id': response.data['receipt_id'],
            'operation_key': operation_key, 'intent_id': intent_id,
            'kind': kind, 'raw_text': response.raw.decode('utf-8'),
            'raw_digest': response.raw_digest,
            'payload_digest': intent['payload_digest'],
            'batch_id': batch_id,
        })
        return True

    # ---------------------------------------------------------- 逐件路线
    def create_item_intent(self, job_id, regulatory):
        snapshot = build_snapshot(self.db, job_id, regulatory)
        package = build_package(snapshot)
        # 同一冻结快照只允许一个未撤回的逐件意图：重复点击不再产生第二份有效备案
        existing = store_mod.latest_intent_for_snapshot(
            self.db, job_id, package['snapshot_version'], route=ROUTE_ITEM)
        if existing:
            return existing['intent_id'], False
        intent_id = self._persist_intent(job_id, ROUTE_ITEM, package)
        request = {'endpoint': '/portal/filings',
                   'idempotency_key': _new_id('idem'),
                   'filing': package}
        self._persist_request(intent_id, 'item-submit', request,
                              request['endpoint'])
        return intent_id, True

    def submit_item(self, job_id, regulatory):
        """交互式逐件提交。崩溃/超时后重入只会重发持久化的原请求。"""
        intent_id, _ = self.create_item_intent(job_id, regulatory)
        return self.dispatch_intent(intent_id)

    def dispatch_intent(self, intent_id):
        intent = store_mod.get_intent(self.db, intent_id)
        if intent['status'] == STATUS_CONFIRMED:
            return intent
        if intent['status'] == STATUS_WITHDRAWN:
            raise FilingError(f'意图 {intent_id} 已随旧版撤回，不能重用其回执')
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='item-submit' "
            'ORDER BY created_at DESC LIMIT 1', (intent_id,)).fetchone()
        operation_key = op['operation_key']
        request = self._stored_request(operation_key)  # 只用冻结的原请求，不看当前 report
        try:
            response = self.portal.submit(request)
        except PortalTransportError as exc:
            store_mod.update_operation_status(self.db, operation_key, 'timeout')
            store_mod.update_intent_status(self.db, intent_id, STATUS_PENDING)
            self._record_query(operation_key, '/portal/filings',
                               f'submit timeout: {exc}', exc.kind)
            return store_mod.get_intent(self.db, intent_id)
        except PortalError as exc:
            return self._handle_portal_error(intent_id, operation_key, exc)
        store_mod.update_operation_status(self.db, operation_key, 'receipted')
        if self._attach_receipt(intent_id, operation_key, response):
            store_mod.update_intent_status(self.db, intent_id, STATUS_CONFIRMED)
        return store_mod.get_intent(self.db, intent_id)

    def _handle_portal_error(self, intent_id, operation_key, exc: PortalError):
        if exc.code == 'duplicate_filing' and exc.existing_receipt_id:
            # 门户侧已有同内容有效备案（例如换了操作键的重复点击）
            response = self._safe_fetch(operation_key, exc.existing_receipt_id)
            if response is None:
                store_mod.update_intent_status(self.db, intent_id, STATUS_PENDING)
                return store_mod.get_intent(self.db, intent_id)
            if self._attach_receipt(intent_id, operation_key, response):
                store_mod.update_intent_status(self.db, intent_id, STATUS_CONFIRMED)
            return store_mod.get_intent(self.db, intent_id)
        store_mod.update_operation_status(self.db, operation_key, f'error:{exc.code}')
        store_mod.update_intent_status(self.db, intent_id, STATUS_CONFLICT)
        store_mod.insert_relation(self.db, {
            'relation_id': _new_id('rel'), 'intent_id': intent_id,
            'kind': 'portal_reject', 'note': f'{exc.code}: {exc}',
        })
        return store_mod.get_intent(self.db, intent_id)

    def reconcile_intent(self, intent_id):
        """崩溃恢复：凭持久化操作键向门户查询，确认“门户已承诺、本地未知”的备案。"""
        intent = store_mod.get_intent(self.db, intent_id)
        if intent['status'] in (STATUS_CONFIRMED, STATUS_WITHDRAWN):
            return intent
        op = self.db.execute(
            'SELECT * FROM filing_operation WHERE intent_id=? '
            'ORDER BY created_at DESC LIMIT 1', (intent_id,)).fetchone()
        operation_key = op['operation_key']
        request = self._stored_request(operation_key)
        key = request['idempotency_key']
        # 第一因果边界：先按操作键直查，绝不用当前 report 重建载荷
        found = None
        try:
            found = self.portal.find_by_idempotency_key(key)
            self._record_query(operation_key, '/portal/filings/by-key', key,
                               found.raw.decode('utf-8') if found else 'null')
        except PortalTransportError as exc:
            self._record_query(operation_key, '/portal/filings/by-key', key, exc.kind)
        if found is None:
            try:
                listing = self.portal.list_filings(intent['job_id'])
                self._record_query(operation_key, '/portal/filings',
                                   intent['job_id'], listing.raw.decode('utf-8'))
            except PortalTransportError as exc:
                # 查询列表延迟：保持待回执，证据已落库
                self._record_query(operation_key, '/portal/filings',
                                   intent['job_id'], exc.kind)
                store_mod.update_intent_status(self.db, intent_id, STATUS_PENDING)
                return store_mod.get_intent(self.db, intent_id)
            match = next((f for f in listing.data['filings']
                          if f['idempotency_key'] == key), None)
            if match:
                found = self._safe_fetch(operation_key, match['receipt_id'])
            else:
                # 同内容但不同操作键的门户在册备案：孤儿风险，转冲突待核
                orphan = next((f for f in listing.data['filings']
                               if f['content_digest'] == intent['payload_digest']
                               and f['status'] == 'active'), None)
                if orphan:
                    store_mod.insert_relation(self.db, {
                        'relation_id': _new_id('rel'), 'intent_id': intent_id,
                        'kind': 'orphan_filing', 'receipt_id': orphan['receipt_id'],
                        'note': '门户存在同内容备案但操作键不匹配，需人工核对',
                    })
                    store_mod.update_intent_status(self.db, intent_id, STATUS_CONFLICT)
                    return store_mod.get_intent(self.db, intent_id)
        if found is None:
            # 门户尚无此备案：原请求可安全重发（操作键不变，门户幂等）
            return self.dispatch_intent(intent_id)
        if self._attach_receipt(intent_id, operation_key, found):
            store_mod.update_operation_status(self.db, operation_key, 'receipted')
            store_mod.update_intent_status(self.db, intent_id, STATUS_CONFIRMED)
        return store_mod.get_intent(self.db, intent_id)

    def _safe_fetch(self, operation_key, receipt_id):
        try:
            response = self.portal.fetch_receipt(receipt_id)
            self._record_query(operation_key, '/portal/receipts', receipt_id,
                               response.raw.decode('utf-8'))
            return response
        except PortalTransportError as exc:
            self._record_query(operation_key, '/portal/receipts',
                               receipt_id, exc.kind)
            return None

    # ---------------------------------------------------------- 撤回协议
    def withdraw_intent(self, intent_id, reason='方案改版，旧签认不可沿用'):
        intent = store_mod.get_intent(self.db, intent_id)
        if intent['status'] == STATUS_WITHDRAWN:
            return intent
        if intent['status'] != STATUS_CONFIRMED:
            # 门户承诺状态未知时不能贸然撤回：先对账
            intent = self.reconcile_intent(intent_id)
            if intent['status'] != STATUS_CONFIRMED:
                raise FilingError(
                    f'意图 {intent_id} 状态为 {intent["status"]}，门户承诺未确认，不能撤回')
        receipt = self.db.execute(
            "SELECT * FROM filing_receipt WHERE intent_id=? AND kind='item' "
            'ORDER BY received_at DESC LIMIT 1', (intent_id,)).fetchone()
        if not receipt:
            raise FilingError('本地无逐件回执，无法发起门户撤回')
        request = {'endpoint': '/portal/withdrawals',
                   'idempotency_key': _new_id('idem'),
                   'receipt_id': receipt['receipt_id'], 'reason': reason}
        operation_key = self._persist_request(
            intent_id, 'withdraw', request, request['endpoint'])
        try:
            response = self.portal.withdraw(request)
        except PortalTransportError as exc:
            store_mod.update_operation_status(self.db, operation_key, 'timeout')
            self._record_query(operation_key, '/portal/withdrawals',
                               receipt['receipt_id'], exc.kind)
            return store_mod.get_intent(self.db, intent_id)
        self._apply_withdrawal(intent_id, operation_key, receipt['receipt_id'],
                               response, reason)
        return store_mod.get_intent(self.db, intent_id)

    def _apply_withdrawal(self, intent_id, operation_key, receipt_id,
                          response, reason):
        store_mod.insert_receipt(self.db, {
            'receipt_id': response.data['withdrawal_id'],
            'operation_key': operation_key, 'intent_id': intent_id,
            'kind': 'withdrawal',
            'raw_text': response.raw.decode('utf-8'),
            'raw_digest': response.raw_digest,
            'payload_digest': response.raw_digest,
        })
        store_mod.insert_relation(self.db, {
            'relation_id': _new_id('rel'), 'intent_id': intent_id,
            'kind': 'withdrawal', 'receipt_id': receipt_id,
            'withdrawal_receipt_id': response.data['withdrawal_id'],
            'note': reason,
        })
        store_mod.update_operation_status(self.db, operation_key, 'withdrawn')
        store_mod.update_intent_status(self.db, intent_id, STATUS_WITHDRAWN)

    def reconcile_withdrawal(self, intent_id):
        """撤回超时后的恢复：查原回执是否已是 withdrawn。"""
        intent = store_mod.get_intent(self.db, intent_id)
        if intent['status'] == STATUS_WITHDRAWN:
            return intent
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='withdraw' "
            'ORDER BY created_at DESC LIMIT 1', (intent_id,)).fetchone()
        if not op:
            return intent
        request = self._stored_request(op['operation_key'])
        try:
            response = self.portal.fetch_receipt(request['receipt_id'])
        except PortalTransportError as exc:
            self._record_query(op['operation_key'], '/portal/receipts',
                               request['receipt_id'], exc.kind)
            return intent
        self._record_query(op['operation_key'], '/portal/receipts',
                           request['receipt_id'], response.raw.decode('utf-8'))
        if response.data['status'] == 'withdrawn':
            recovered = _RecoveredWithdrawal(request['idempotency_key'],
                                             request['receipt_id'])
            self._apply_withdrawal(intent_id, op['operation_key'],
                                   request['receipt_id'], recovered,
                                   request.get('reason', ''))
        return store_mod.get_intent(self.db, intent_id)

    # ---------------------------------------------------------- 夜间批量路线
    def submit_batch(self, jobs):
        """jobs: [{'job_id':..., 'regulatory':...}, ...]。内容与逐件路线相同，粒度不同。"""
        if not jobs:
            raise FilingError('批量封包清单为空')
        manifest_id = _new_id('batch')
        packages = []
        for entry in jobs:
            snapshot = build_snapshot(self.db, entry['job_id'], entry['regulatory'])
            packages.append((entry['job_id'], build_package(snapshot)))
        manifest = {
            'manifest_id': manifest_id, 'item_count': len(packages),
            'items': [{'item_key': job_id,
                       'job_id': package['job_id'],
                       'snapshot_version': package['snapshot_version'],
                       'content_digest': package['content_digest'],
                       'filing': package}
                      for job_id, package in packages],
        }
        manifest['manifest_digest'] = digest(manifest)
        envelope = {'package_type': 'batch-envelope', 'manifest_id': manifest_id,
                    'snapshot_version': manifest['manifest_digest'],
                    'content_digest': manifest['manifest_digest'],
                    'manifest': manifest}
        parent_id = self._persist_intent(
            ','.join(job_id for job_id, _ in packages), ROUTE_BATCH, envelope)
        children = []
        for job_id, package in packages:
            child_id = self._persist_intent(
                job_id, ROUTE_BATCH, package, parent=parent_id, item_key=job_id)
            children.append(child_id)
            store_mod.insert_relation(self.db, {
                'relation_id': _new_id('rel'), 'intent_id': child_id,
                'kind': 'batch-member', 'related_intent_id': parent_id,
                'note': f'批次清单项 {job_id}',
            })
        request = {'endpoint': '/portal/batches',
                   'idempotency_key': _new_id('idem'), 'manifest': manifest}
        operation_key = self._persist_request(
            parent_id, 'batch-submit', request, request['endpoint'])
        try:
            response = self.portal.submit_batch(request)
        except PortalTransportError as exc:
            store_mod.update_operation_status(self.db, operation_key, 'timeout')
            store_mod.update_intent_status(self.db, parent_id, STATUS_PENDING)
            for child_id in children:
                store_mod.update_intent_status(self.db, child_id, STATUS_PENDING)
            self._record_query(operation_key, '/portal/batches',
                               manifest_id, exc.kind)
            return parent_id, children
        self._apply_batch_receipt(parent_id, children, operation_key, response)
        return parent_id, children

    def _apply_batch_receipt(self, parent_id, children, operation_key, response):
        batch_id = response.data['batch_id']
        store_mod.insert_receipt(self.db, {
            'receipt_id': batch_id, 'operation_key': operation_key,
            'intent_id': parent_id, 'kind': 'batch',
            'raw_text': response.raw.decode('utf-8'),
            'raw_digest': response.raw_digest,
            'payload_digest': response.data['manifest_digest'],
            'batch_id': batch_id,
        })
        store_mod.update_operation_status(self.db, operation_key, 'sealed')
        # B5 只证明批次已收：逐件确认前，父子都只能是“批次已收、逐件待核”
        for intent_id in [parent_id, *children]:
            store_mod.update_intent_status(self.db, intent_id, STATUS_BATCH_PENDING)
            self.db.execute('UPDATE filing_intent SET batch_id=? WHERE intent_id=?',
                            (batch_id, intent_id))
        self.db.commit()

    def reconcile_batch(self, parent_id):
        """展开批次逐件回执。另一件失败不能推断本件成败，逐件独立标记。"""
        op = self.db.execute(
            "SELECT * FROM filing_operation WHERE intent_id=? AND kind='batch-submit' "
            'ORDER BY created_at DESC LIMIT 1', (parent_id,)).fetchone()
        children = [dict(r) for r in self.db.execute(
            'SELECT * FROM filing_intent WHERE parent_intent_id=? ORDER BY created_at',
            (parent_id,)).fetchall()]
        batch_receipt = self.db.execute(
            "SELECT * FROM filing_receipt WHERE intent_id=? AND kind='batch' "
            'ORDER BY received_at DESC LIMIT 1', (parent_id,)).fetchone()
        if not batch_receipt:
            # 批次提交曾超时：原封包按原操作键幂等重放，门户返回同一个批次回执
            request = self._stored_request(op['operation_key'])
            try:
                response = self.portal.submit_batch(request)
            except PortalTransportError as exc:
                self._record_query(op['operation_key'], '/portal/batches',
                                   request['idempotency_key'], exc.kind)
                return parent_id
            self._apply_batch_receipt(parent_id, [c['intent_id'] for c in children],
                                      op['operation_key'], response)
            batch_receipt = self.db.execute(
                "SELECT * FROM filing_receipt WHERE intent_id=? AND kind='batch' "
                'ORDER BY received_at DESC LIMIT 1', (parent_id,)).fetchone()
        batch_id = batch_receipt['batch_id']
        try:
            expansion = self.portal.expand_batch(batch_id)
        except PortalTransportError as exc:
            self._record_query(op['operation_key'], '/portal/batches/expand',
                               batch_id, exc.kind)
            return parent_id
        self._record_query(op['operation_key'], '/portal/batches/expand',
                           batch_id, expansion.raw.decode('utf-8'))
        by_key = {i['item_key']: i for i in expansion.data['items']}
        for child in children:
            item = by_key.get(child['item_key'])
            if not item:
                continue
            if item['status'] == 'failed':
                store_mod.update_intent_status(self.db, child['intent_id'],
                                               STATUS_CONFLICT)
                store_mod.insert_relation(self.db, {
                    'relation_id': _new_id('rel'),
                    'intent_id': child['intent_id'], 'kind': 'batch_item_failed',
                    'note': '批量逐件展开失败，B5 不代表该件成败，需重新逐件备案',
                })
                continue
            response = self.portal.fetch_receipt(item['receipt_id'])
            if self._attach_receipt(child['intent_id'], op['operation_key'],
                                    response, kind='item', batch_id=batch_id):
                store_mod.update_intent_status(self.db, child['intent_id'],
                                               STATUS_CONFIRMED)
        still_pending = self.db.execute(
            'SELECT COUNT(*) FROM filing_intent WHERE parent_intent_id=? '
            "AND status=?", (parent_id, STATUS_BATCH_PENDING)).fetchone()[0]
        if still_pending == 0:
            store_mod.update_intent_status(self.db, parent_id, STATUS_CONFIRMED)
        return parent_id

    def withdraw_batch_item(self, child_intent_id, reason='方案改版，旧批次件撤回'):
        child = store_mod.get_intent(self.db, child_intent_id)
        if child['route'] != ROUTE_BATCH or not child['batch_id']:
            raise FilingError('该意图不是批次清单项')
        request = {'endpoint': '/portal/batch-withdrawals',
                   'idempotency_key': _new_id('idem'),
                   'batch_id': child['batch_id'],
                   'item_key': child['item_key'], 'reason': reason}
        operation_key = self._persist_request(
            child_intent_id, 'batch-withdraw', request, request['endpoint'])
        try:
            response = self.portal.withdraw_batch_item(request)
        except PortalTransportError as exc:
            store_mod.update_operation_status(self.db, operation_key, 'timeout')
            self._record_query(operation_key, '/portal/batch-withdrawals',
                               child['item_key'], exc.kind)
            return child
        store_mod.insert_receipt(self.db, {
            'receipt_id': response.data['withdrawal_id'],
            'operation_key': operation_key, 'intent_id': child_intent_id,
            'kind': 'withdrawal',
            'raw_text': response.raw.decode('utf-8'),
            'raw_digest': response.raw_digest,
            'payload_digest': response.raw_digest,
            'batch_id': child['batch_id'],
        })
        store_mod.insert_relation(self.db, {
            'relation_id': _new_id('rel'), 'intent_id': child_intent_id,
            'kind': 'withdrawal', 'note': reason,
        })
        store_mod.update_intent_status(self.db, child_intent_id, STATUS_WITHDRAWN)
        return store_mod.get_intent(self.db, child_intent_id)

    # ---------------------------------------------------------- 调度端查询
    def ledger(self, status=None, job_id=None, route=None):
        return store_mod.filing_ledger(self.db, status=status, job_id=job_id,
                                       route=route)


class _RecoveredWithdrawal:
    """撤回恢复时门户未重发撤回回执、仅原回执状态变 withdrawn 的本地替代封装。"""

    def __init__(self, idempotency_key, receipt_id):
        self.data = {'receipt_type': 'withdrawal',
                     'withdrawal_id': f'W-recovered-{receipt_id}',
                     'receipt_id': receipt_id,
                     'idempotency_key': idempotency_key,
                     'status': 'withdrawn'}
        self.raw = canonical_bytes(self.data)
        self.raw_digest = digest(self.raw)
