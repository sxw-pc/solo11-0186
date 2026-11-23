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
CREATE TABLE IF NOT EXISTS capture_batch (capture_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, version INTEGER NOT NULL, plan_version INTEGER, geometry_sha TEXT, captured_at TEXT, status TEXT NOT NULL, verified INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capture_reading (capture_id TEXT NOT NULL, sample_id TEXT NOT NULL, meter_uid TEXT NOT NULL, sling_rfid TEXT, hook_id TEXT, stage TEXT NOT NULL, at TEXT NOT NULL, raw_n REAL, zero_n REAL, range_n REAL, kgf REAL, unit TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(capture_id,sample_id));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
"""

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
    db.execute('DELETE FROM capture_batch')
    db.execute('DELETE FROM capture_reading')
    for name,item in sorted(decisions.items()):
        db.execute('INSERT INTO decisions VALUES (?,?,?,?)',(name,item['result'],item['reason'],json.dumps(item['evidence'],ensure_ascii=False,sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES (?,?,?,?)',(revision,name,item['result'],item['reason']))
    for subsystem,entity,state,version in states:
        db.execute('INSERT INTO entity_state VALUES (?,?,?,?)',(subsystem,entity,state,version))
    for event in events:
        for subsystem,entity_type,entity_id,body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)',(subsystem,entity_type,entity_id,event['id'],event.get('version',1),json.dumps(body,ensure_ascii=False,sort_keys=True)))
    _materialize_captures(db, events)


def _materialize_captures(db, events):
    """Persist capture batches and every converted sample for audit/report."""
    from . import tension_capture
    plans = rules.plans.evaluate([e for e in events if e['kind'] == 'plan'])
    assets = rules.resources.evaluate([e for e in events if e['kind'] == 'asset'], plans)
    chosen = rules._captures_for(events, plans, assets)
    batches = rules._choose_capture([e for e in events if e['kind'] == 'sling_capture'])
    for job, capture in chosen.items():
        batch = batches[job]
        db.execute('INSERT OR REPLACE INTO capture_batch VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (batch.get('capture_id'), job, int(batch.get('version', 1)),
                    batch.get('plan_version'), batch.get('geometry_sha'),
                    batch.get('captured_at'), capture['status'],
                    int(capture['verified']), capture['digest'],
                    json.dumps(batch, ensure_ascii=False, sort_keys=True)))
        for reading in batch.get('events', []):
            db.execute('INSERT OR REPLACE INTO capture_reading VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (batch.get('capture_id'), reading.get('id'), reading.get('meter_uid'),
                        reading.get('sling_rfid'), reading.get('hook_id'),
                        reading.get('stage', ''), reading.get('at', ''),
                        reading.get('value_n'), reading.get('zero_n'),
                        reading.get('range_n'), tension_capture.to_kgf(reading),
                        (reading.get('calibration') or {}).get('unit', 'N'),
                        json.dumps(reading, ensure_ascii=False, sort_keys=True)))

def report(db):
    return {'decisions':{r['name']:{'result':r['result'],'reason':r['reason'],'evidence':json.loads(r['evidence'])} for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'entities':[dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records':[dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'captures':[dict(r) for r in db.execute('SELECT capture_id,job_id,version,plan_version,geometry_sha,captured_at,status,verified,digest FROM capture_batch ORDER BY capture_id')],
            'capture_readings':[dict(r) for r in db.execute('SELECT capture_id,sample_id,meter_uid,sling_rfid,hook_id,stage,at,raw_n,zero_n,range_n,kgf,unit FROM capture_reading ORDER BY capture_id,sample_id')],
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
