import hashlib
import json
import sqlite3
from pathlib import Path

from . import rules

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (name TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_state (subsystem TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subsystem,entity_id));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,name));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
CREATE TABLE IF NOT EXISTS filing_intent (
    intent_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL,
    parent_intent_id TEXT,
    snapshot_version TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    batch_id TEXT,
    item_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_operation (
    operation_key TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request_text TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    portal_endpoint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_receipt (
    receipt_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    raw_digest TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    batch_id TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY(receipt_id, intent_id)
);
CREATE TABLE IF NOT EXISTS filing_query (
    query_id TEXT PRIMARY KEY,
    operation_key TEXT,
    endpoint TEXT NOT NULL,
    query_text TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_relation (
    relation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    related_intent_id TEXT,
    receipt_id TEXT,
    withdrawal_receipt_id TEXT,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

FILING_STATUSES = (
    '待提交', '提交待回执', '已提交待回执', '批次已收逐件待核',
    '已确认', '冲突待核', '旧版已撤回',
)

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db

def import_file(db, kind, file):
    raw=Path(file).read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    payload=json.loads(raw)
    if not isinstance(payload,list) or not payload:
        raise ValueError('source must contain a nonempty event list')
    if db.execute('SELECT 1 FROM source_files WHERE sha=?',(sha,)).fetchone():
        return {'sha':sha,'inserted':0,'idempotent':True}
    db.execute('SAVEPOINT ingest')
    try:
        seq=db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        for event in payload:
            if event.get('kind')!=kind or not event.get('id'):
                raise ValueError('event kind/id mismatch')
            body=json.dumps(event,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT body FROM events WHERE event_id=?',(event['id'],)).fetchone()
            if old:
                if old[0]!=body: raise ValueError('same event id with different content')
                continue
            seq+=1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',(event['id'],kind,body,sha,seq))
        db.execute('INSERT INTO source_files(sha,kind,raw) VALUES (?,?,?)',(sha,kind,raw))
        rebuild(db)
        db.execute('RELEASE SAVEPOINT ingest')
        db.commit()
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT ingest')
        db.execute('RELEASE SAVEPOINT ingest')
        db.rollback()
        raise
    return {'sha':sha,'inserted':len(payload),'idempotent':False}

def rebuild(db):
    events=[json.loads(row[0]) for row in db.execute('SELECT body FROM events ORDER BY seq')]
    decisions,states=rules.calculate(events)
    revision=db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0]+1
    db.execute('DELETE FROM decisions')
    db.execute('DELETE FROM entity_state')
    db.execute('DELETE FROM business_record')
    for name,item in sorted(decisions.items()):
        db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(name,item['result'],item['reason'],json.dumps(item['evidence'],ensure_ascii=False,sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES (?,?,?,?)',(revision,name,item['result'],item['reason']))
    for subsystem,entity,state,version in states:
        db.execute('INSERT INTO entity_state VALUES (?,?,?,?)',(subsystem,entity,state,version))
    for event in events:
        for subsystem,entity_type,entity_id,body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)',(subsystem,entity_type,entity_id,event['id'],event.get('version',1),json.dumps(body,ensure_ascii=False,sort_keys=True)))

def report(db):
    return {'decisions':{r['name']:{'result':r['result'],'reason':r['reason'],'evidence':json.loads(r['evidence'])} for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'entities':[dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records':[dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'sources':[dict(r) for r in db.execute('SELECT sha,kind FROM source_files ORDER BY sha')]}

def audit(db):
    before=report(db)
    source_rows=list(db.execute('SELECT kind,raw FROM source_files ORDER BY rowid'))
    memory=connect(':memory:')
    for kind,raw in source_rows:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw); path=tmp.name
        try: import_file(memory,kind,path)
        finally: Path(path).unlink()
    after=report(memory)
    return {'ok':before==after,'source_count':len(source_rows),'decision_count':len(before['decisions'])}

# ---------------------------------------------------------------------------
# 备案账持久化：以下事务只覆盖本地 SQLite。门户已接受的备案不可能随本地回滚，
# 任何门户交互都先在本地落提交意图/操作键，再做外部调用，重放时只重发原请求。
# ---------------------------------------------------------------------------

def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def get_intent(db, intent_id):
    row = db.execute('SELECT * FROM filing_intent WHERE intent_id=?', (intent_id,)).fetchone()
    return dict(row) if row else None


def get_operation(db, operation_key):
    row = db.execute('SELECT * FROM filing_operation WHERE operation_key=?',
                     (operation_key,)).fetchone()
    return dict(row) if row else None


def get_receipt(db, receipt_id):
    row = db.execute('SELECT * FROM filing_receipt WHERE receipt_id=?',
                     (receipt_id,)).fetchone()
    return dict(row) if row else None


def insert_intent(db, intent):
    now = _now()
    db.execute(
        'INSERT INTO filing_intent VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (intent['intent_id'], intent['job_id'], intent['route'], intent['status'],
         intent.get('parent_intent_id'), intent['snapshot_version'],
         json.dumps(intent['payload'], ensure_ascii=False, sort_keys=True),
         intent['payload_digest'], intent.get('batch_id'), intent.get('item_key'),
         now, now))
    db.commit()


def update_intent_status(db, intent_id, status):
    db.execute('UPDATE filing_intent SET status=?, updated_at=? WHERE intent_id=?',
               (status, _now(), intent_id))
    db.commit()


def insert_operation(db, op):
    now = _now()
    db.execute('INSERT INTO filing_operation VALUES (?,?,?,?,?,?,?,?,?)',
               (op['operation_key'], op['intent_id'], op['kind'], op['status'],
                op['request_text'], op['request_digest'], op.get('portal_endpoint'),
                now, now))
    db.commit()


def update_operation_status(db, operation_key, status):
    db.execute('UPDATE filing_operation SET status=?, updated_at=? WHERE operation_key=?',
               (status, _now(), operation_key))
    db.commit()


def insert_receipt(db, receipt):
    db.execute('INSERT INTO filing_receipt VALUES (?,?,?,?,?,?,?,?,?)',
               (receipt['receipt_id'], receipt['operation_key'], receipt['intent_id'],
                receipt['kind'], receipt['raw_text'], receipt['raw_digest'],
                receipt['payload_digest'], receipt.get('batch_id'),
                receipt.get('received_at', _now())))
    db.commit()


def insert_query(db, evidence):
    db.execute('INSERT INTO filing_query VALUES (?,?,?,?,?,?)',
               (evidence['query_id'], evidence.get('operation_key'), evidence['endpoint'],
                evidence['query_text'], evidence['raw_text'],
                evidence.get('observed_at', _now())))
    db.commit()


def insert_relation(db, relation):
    db.execute('INSERT INTO filing_relation VALUES (?,?,?,?,?,?,?,?)',
               (relation['relation_id'], relation['intent_id'], relation['kind'],
                relation.get('related_intent_id'), relation.get('receipt_id'),
                relation.get('withdrawal_receipt_id'), relation['note'],
                relation.get('created_at', _now())))
    db.commit()


def latest_intent_for_snapshot(db, job_id, snapshot_version, route=None):
    """同一作业同一快照版本只允许一个活跃逐件意图，阻止重复点击产生第二份有效备案。"""
    sql = ("SELECT * FROM filing_intent WHERE job_id=? AND snapshot_version=? "
           "AND status != '旧版已撤回'")
    params = [job_id, snapshot_version]
    if route:
        sql += ' AND route=?'
        params.append(route)
    sql += ' ORDER BY created_at DESC'
    rows = db.execute(sql, params).fetchall()
    return dict(rows[0]) if rows else None


def filing_ledger(db, status=None, job_id=None, route=None):
    """调度端回执账：状态、本地裁定版本与门户操作键/回执一一可核对。"""
    where, params = [], []
    if status:
        where.append('i.status=?'); params.append(status)
    if job_id:
        where.append('i.job_id=?'); params.append(job_id)
    if route:
        where.append('i.route=?'); params.append(route)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    rows = db.execute(
        'SELECT i.* FROM filing_intent i' + clause + ' ORDER BY i.created_at', params).fetchall()
    ledger = []
    for row in rows:
        item = dict(row)
        item['payload'] = json.loads(item['payload'])
        item['operations'] = [dict(r) for r in db.execute(
            'SELECT operation_key,kind,status,portal_endpoint,created_at,updated_at '
            'FROM filing_operation WHERE intent_id=? ORDER BY created_at', (item['intent_id'],))]
        item['receipts'] = [dict(r) for r in db.execute(
            'SELECT receipt_id,kind,raw_digest,payload_digest,batch_id,received_at '
            'FROM filing_receipt WHERE intent_id=? ORDER BY received_at', (item['intent_id'],))]
        item['relations'] = [dict(r) for r in db.execute(
            'SELECT relation_id,kind,related_intent_id,receipt_id,withdrawal_receipt_id,note '
            'FROM filing_relation WHERE intent_id=? ORDER BY created_at', (item['intent_id'],))]
        item['queries'] = [dict(r) for r in db.execute(
            'SELECT query_id,operation_key,endpoint,observed_at FROM filing_query '
            'WHERE operation_key IN (SELECT operation_key FROM filing_operation '
            'WHERE intent_id=?) ORDER BY observed_at', (item['intent_id'],))]
        ledger.append(item)
    return ledger
