"""离线模拟监管门户（含 RPA 传输层故障注入）。

门户状态独立于采录端 SQLite，保存在单独的 JSON 文件里；这从物理上保证
“本地一个事务回滚不掉门户已承诺的备案”。所有响应都同时返回解析后的字典和
规范化原文（canonical bytes），采录端必须原样留存回执字节，回执与备案快照
按摘要一一对应。

故障注入均为显式、一次性的脚本（faults），便于在固定时序的中断点恢复测试：
- submit_timeout:<idempotency_key>  门户已生成回执，但传输层超时
- receipt_unavailable:<receipt_id>  回执页跳转失败（备案仍在册）
- list_delay                       查询列表延迟，暂时看不到新备案
- batch_item_fail:<item_key>       夜间批量中某一件展开失败
- batch_expand_pending:<batch_id>  批量逐件回执暂不展开
- withdraw_timeout:<receipt_id>    撤回请求超时但门户实际已撤回
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path


def canonical_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(obj) -> str:
    if isinstance(obj, (bytes, bytearray)):
        return hashlib.sha256(bytes(obj)).hexdigest()
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


class PortalError(Exception):
    """门户确定性拒绝（4xx 语义），重试不会改变结果。"""

    def __init__(self, code, message, *, existing_receipt_id=None):
        super().__init__(message)
        self.code = code
        self.existing_receipt_id = existing_receipt_id


class PortalTransportError(Exception):
    """传输层故障（超时/跳转失败）。门户可能已经处理，必须凭操作键查询，禁止重建载荷重发。"""

    def __init__(self, message, *, kind='timeout'):
        super().__init__(message)
        self.kind = kind


class PortalResponse:
    def __init__(self, data):
        self.data = data
        self.raw = canonical_bytes(data)
        self.raw_digest = hashlib.sha256(self.raw).hexdigest()


class MockPortal:
    """线程无关的进程内门户；path 给定时每次变更落盘，模拟独立系统。"""

    def __init__(self, path=None, item_start=76, batch_start=4,
                 withdrawal_start=0, faults=None):
        """首个逐件回执默认 R77、首个批次回执默认 B5，贴合 fixtures/filing-crash.json。"""
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._faults = list(faults or [])
        self._counters = {'item': item_start, 'batch': batch_start,
                         'withdrawal': withdrawal_start, 'query': 0}
        self._filings = {}          # receipt_id -> 备案记录
        self._idempotency = {}      # idempotency_key -> receipt_id
        self._batches = {}          # batch_id -> 批次记录
        self._withdrawn = set()     # 已撤回回执 receipt_id
        self._withdrawals = {}      # receipt_id -> withdrawal receipt
        self._list_visible_until = 0
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------- 持久化
    def _load(self):
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self._counters = state['counters']
        self._filings = state['filings']
        self._idempotency = state['idempotency']
        self._batches = state['batches']
        self._withdrawn = set(state['withdrawn'])
        self._withdrawals = state['withdrawals']
        self._list_visible_until = state.get('list_visible_until', 0)

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({
            'counters': self._counters, 'filings': self._filings,
            'idempotency': self._idempotency, 'batches': self._batches,
            'withdrawn': sorted(self._withdrawn), 'withdrawals': self._withdrawals,
            'list_visible_until': self._list_visible_until,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def _next_id(self, kind, prefix):
        self._counters[kind] += 1
        return f'{prefix}{self._counters[kind]}'

    def _take_fault(self, needle):
        for i, script in enumerate(self._faults):
            if script == needle or script.startswith(needle + ':'):
                del self._faults[i]
                return script
        return None

    def inject_faults(self, faults):
        self._faults.extend(faults)

    # ------------------------------------------------------------- 逐件备案
    def submit(self, request: dict) -> PortalResponse:
        """逐件提交。request 含 idempotency_key 与 filing 备案包。"""
        key = request.get('idempotency_key')
        filing = request.get('filing') or {}
        if not key or not filing:
            raise PortalError('bad_request', '缺少 idempotency_key 或 filing 备案包')
        with self._lock:
            if key in self._idempotency:
                receipt_id = self._idempotency[key]
                record = self._filings[receipt_id]
                if record['request_digest'] != digest(request):
                    raise PortalError('idempotency_conflict',
                                      '同一操作键对应不同请求，门户拒绝重放')
                return self._receipt_response(receipt_id)
            # 同键不同键位的重复内容（用户重复点击换了操作键）：报 409 并指回原回执
            duplicate = self._find_active_filing(filing.get('job_id'),
                                                 filing.get('content_digest'))
            timed_out = self._take_fault(f'submit_timeout:{key}')
            receipt_id = self._next_id('item', 'R')
            record = {
                'receipt_id': receipt_id,
                'idempotency_key': key,
                'job_id': filing.get('job_id'),
                'snapshot_version': filing.get('snapshot_version'),
                'content_digest': filing.get('content_digest'),
                'request_digest': digest(request),
                'filing': filing,
                'status': 'active',
                'seq': len(self._filings),
            }
            self._filings[receipt_id] = record
            self._idempotency[key] = receipt_id
            self._save()
            if timed_out:
                raise PortalTransportError(
                    f'门户已接收备案 {receipt_id}，但响应超时', kind='timeout')
            if duplicate:
                raise PortalError('duplicate_filing',
                                  f'相同内容已有有效备案 {duplicate}',
                                  existing_receipt_id=duplicate)
            return self._receipt_response(receipt_id)

    def _find_active_filing(self, job_id, content_digest):
        for rid, record in self._filings.items():
            if (record['job_id'] == job_id
                    and record['content_digest'] == content_digest
                    and rid not in self._withdrawn):
                return rid
        return None

    def _receipt_response(self, receipt_id):
        record = self._filings[receipt_id]
        receipt = {
            'receipt_type': 'item',
            'receipt_id': receipt_id,
            'idempotency_key': record['idempotency_key'],
            'job_id': record['job_id'],
            'snapshot_version': record['snapshot_version'],
            'content_digest': record['content_digest'],
            'status': 'withdrawn' if receipt_id in self._withdrawn else 'active',
            'seal': hashlib.sha256(
                (receipt_id + record['content_digest']).encode()).hexdigest()[:16],
        }
        return PortalResponse(receipt)

    def fetch_receipt(self, receipt_id) -> PortalResponse:
        with self._lock:
            if receipt_id not in self._filings:
                raise PortalError('not_found', f'门户无此回执 {receipt_id}')
            if self._take_fault(f'receipt_unavailable:{receipt_id}'):
                raise PortalTransportError(f'回执 {receipt_id} 页面跳转失败',
                                           kind='jump_failure')
            return self._receipt_response(receipt_id)

    # ------------------------------------------------------------- 列表查询
    def list_filings(self, job_id=None) -> PortalResponse:
        with self._lock:
            self._counters['query'] += 1
            if self._take_fault('list_delay'):
                raise PortalTransportError('门户查询列表延迟，请稍后重试',
                                           kind='delayed')
            visible = []
            for rid, record in sorted(self._filings.items(),
                                      key=lambda kv: kv[1]['seq']):
                if job_id and record['job_id'] != job_id:
                    continue
                visible.append({
                    'receipt_id': rid,
                    'idempotency_key': record['idempotency_key'],
                    'job_id': record['job_id'],
                    'snapshot_version': record['snapshot_version'],
                    'content_digest': record['content_digest'],
                    'status': 'withdrawn' if rid in self._withdrawn else 'active',
                })
            return PortalResponse({'receipt_type': 'listing', 'filings': visible})

    def find_by_idempotency_key(self, key) -> PortalResponse | None:
        with self._lock:
            if self._take_fault(f'key_lookup_unavailable:{key}'):
                raise PortalTransportError('操作键查询通道不可用，请稍后重试',
                                           kind='jump_failure')
            rid = self._idempotency.get(key)
            return self._receipt_response(rid) if rid else None

    # ------------------------------------------------------------- 撤回协议
    def withdraw(self, request: dict) -> PortalResponse:
        key = request.get('idempotency_key')
        receipt_id = request.get('receipt_id')
        reason = request.get('reason', '')
        if not key or not receipt_id:
            raise PortalError('bad_request', '撤回缺少操作键或回执号')
        with self._lock:
            if receipt_id not in self._filings:
                raise PortalError('not_found', f'门户无此回执 {receipt_id}')
            prior = self._withdrawals.get(receipt_id)
            if prior:
                if prior['idempotency_key'] != key:
                    raise PortalError('idempotency_conflict', '该回执已用另一操作键撤回')
                return PortalResponse(prior)
            timed_out = self._take_fault(f'withdraw_timeout:{receipt_id}')
            wid = self._next_id('withdrawal', 'W')
            receipt = {
                'receipt_type': 'withdrawal',
                'withdrawal_id': wid,
                'receipt_id': receipt_id,
                'idempotency_key': key,
                'job_id': self._filings[receipt_id]['job_id'],
                'reason': reason,
                'status': 'withdrawn',
            }
            self._withdrawn.add(receipt_id)
            self._withdrawals[receipt_id] = receipt
            self._save()
            if timed_out:
                raise PortalTransportError(
                    f'门户已撤回 {receipt_id}，但撤回响应超时', kind='timeout')
            return PortalResponse(receipt)

    # ------------------------------------------------------------- 夜间签章批量
    def submit_batch(self, request: dict) -> PortalResponse:
        key = request.get('idempotency_key')
        manifest = request.get('manifest') or {}
        items = manifest.get('items') or []
        if not key or not items:
            raise PortalError('bad_request', '批量封包缺少操作键或清单')
        with self._lock:
            existing = self._batches.get(key)
            if existing:
                if existing['request_digest'] != digest(request):
                    raise PortalError('idempotency_conflict',
                                      '同一批次操作键对应不同封包')
                return PortalResponse(self._batch_receipt(existing['batch_id']))
            batch_id = self._next_id('batch', 'B')
            batch = {
                'batch_id': batch_id,
                'idempotency_key': key,
                'request_digest': digest(request),
                'manifest_digest': manifest.get('manifest_digest'),
                'items': [],
            }
            for item in items:
                fail = self._take_fault(f"batch_item_fail:{item['item_key']}")
                batch['items'].append({
                    'item_key': item['item_key'],
                    'job_id': item['filing']['job_id'],
                    'snapshot_version': item['filing']['snapshot_version'],
                    'content_digest': item['filing']['content_digest'],
                    'filing': item['filing'],
                    'expand_status': 'failed' if fail else 'pending',
                    'receipt_id': None,
                })
            self._batches[key] = batch
            self._save()
            if self._take_fault(f'submit_timeout:{key}'):
                raise PortalTransportError(
                    f'门户已收批次 {batch_id}，但响应超时', kind='timeout')
            return PortalResponse(self._batch_receipt(batch_id))

    def _batch_receipt(self, batch_id):
        batch = next(b for b in self._batches.values()
                     if b['batch_id'] == batch_id)
        return {
            'receipt_type': 'batch',
            'batch_id': batch_id,
            'idempotency_key': batch['idempotency_key'],
            'manifest_digest': batch['manifest_digest'],
            'item_count': len(batch['items']),
            'status': 'sealed',
        }

    def expand_batch(self, batch_id) -> PortalResponse:
        """逐件展开回执。显式 pending 故障期间保持“批次已收、逐件待核”。"""
        with self._lock:
            batch = next((b for b in self._batches.values()
                          if b['batch_id'] == batch_id), None)
            if not batch:
                raise PortalError('not_found', f'门户无此批次 {batch_id}')
            if self._take_fault(f'batch_expand_pending:{batch_id}'):
                raise PortalTransportError(f'批次 {batch_id} 逐件回执尚未生成',
                                           kind='delayed')
            expanded = []
            for item in batch['items']:
                if item['expand_status'] == 'failed':
                    expanded.append({'item_key': item['item_key'],
                                     'job_id': item['job_id'],
                                     'status': 'failed', 'receipt_id': None})
                    continue
                if item['receipt_id'] is None:
                    rid = self._next_id('item', 'R')
                    self._filings[rid] = {
                        'receipt_id': rid,
                        'idempotency_key': f"{batch['idempotency_key']}:{item['item_key']}",
                        'job_id': item['job_id'],
                        'snapshot_version': item['snapshot_version'],
                        'content_digest': item['content_digest'],
                        'request_digest': digest(item['filing']),
                        'filing': item['filing'],
                        'status': 'active',
                        'seq': len(self._filings),
                    }
                    self._idempotency[
                        f"{batch['idempotency_key']}:{item['item_key']}"] = rid
                    item['receipt_id'] = rid
                    item['expand_status'] = 'expanded'
                expanded.append({'item_key': item['item_key'],
                                 'job_id': item['job_id'],
                                 'status': item['expand_status'],
                                 'receipt_id': item['receipt_id'],
                                 'content_digest': item['content_digest']})
            self._save()
            return PortalResponse({'receipt_type': 'batch_expansion',
                                   'batch_id': batch_id, 'items': expanded})

    def withdraw_batch_item(self, request: dict) -> PortalResponse:
        """批量件撤回：逐件回执已生成则撤回执；未展开则只把该件标撤回。"""
        key = request.get('idempotency_key')
        batch_id = request.get('batch_id')
        item_key = request.get('item_key')
        with self._lock:
            batch = next((b for b in self._batches.values()
                          if b['batch_id'] == batch_id), None)
            if not batch:
                raise PortalError('not_found', f'门户无此批次 {batch_id}')
            item = next((i for i in batch['items']
                         if i['item_key'] == item_key), None)
            if not item:
                raise PortalError('not_found', f'批次无此件 {item_key}')
            if item['expand_status'] == 'withdrawn':
                return PortalResponse(self._withdrawals[item['receipt_id']])
            if item['receipt_id']:
                sub = {'idempotency_key': key,
                       'receipt_id': item['receipt_id'],
                       'reason': request.get('reason', '')}
                resp = self.withdraw(sub)
                item['expand_status'] = 'withdrawn'
                self._save()
                return resp
            item['expand_status'] = 'withdrawn'
            wid = self._next_id('withdrawal', 'W')
            receipt = {'receipt_type': 'withdrawal', 'withdrawal_id': wid,
                       'receipt_id': None, 'batch_id': batch_id,
                       'item_key': item_key, 'idempotency_key': key,
                       'job_id': item['job_id'],
                       'reason': request.get('reason', ''),
                       'status': 'withdrawn'}
            self._withdrawals[f'{batch_id}:{item_key}'] = receipt
            self._save()
            return PortalResponse(receipt)
