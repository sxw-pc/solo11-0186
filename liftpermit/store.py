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
CREATE TABLE IF NOT EXISTS vendor_page (sha TEXT PRIMARY KEY,doc_id TEXT NOT NULL,page_no INTEGER NOT NULL,revision TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS vendor_capture (
    portal_id TEXT NOT NULL, doc_id TEXT NOT NULL, revision TEXT NOT NULL,
    page_no INTEGER NOT NULL, sha256 TEXT NOT NULL, etag TEXT NOT NULL,
    signature TEXT NOT NULL, raw BLOB NOT NULL, fetched_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(portal_id,doc_id,revision,page_no,sha256));
CREATE TABLE IF NOT EXISTS vendor_snapshot (
    snapshot_id TEXT PRIMARY KEY, portal_id TEXT NOT NULL, doc_id TEXT NOT NULL,
    revision TEXT NOT NULL, expected_pages INTEGER NOT NULL,
    captured_pages INTEGER NOT NULL, state TEXT NOT NULL, gaps TEXT NOT NULL,
    coverage TEXT NOT NULL, raw_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL, evidence_kind TEXT NOT NULL DEFAULT 'scan',
    current INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS vendor_snapshot_page (
    snapshot_id TEXT NOT NULL, page_no INTEGER NOT NULL, sha256 TEXT NOT NULL,
    signature TEXT NOT NULL, etag TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,page_no));
CREATE TABLE IF NOT EXISTS asset_rating_curve (
    crane_id TEXT NOT NULL, revision TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    rows TEXT NOT NULL, coverage TEXT NOT NULL, evidence TEXT NOT NULL,
    published_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS asset_import (
    event_id TEXT PRIMARY KEY, sha TEXT NOT NULL, job_id TEXT,
    rating_provenance TEXT NOT NULL, verdict TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

PROVENANCE_ATTESTED = "attested"
PROVENANCE_HISTORICAL = "historical"
PROVENANCE_UNCERTIFIED = "uncertified_provenance"
PROVENANCE_UNTRUSTED = "untrusted_provenance"
PROVENANCE_ABSENT = "absent"

# A curve is publication evidence only if its snapshot was certified and
# the curve was written by ``publish_snapshot``; see ``provenance_verdict``.



def provenance_verdict(db, event):
    """Decide whether an asset event's rating curve is trustworthy.

    * No ``rating_provenance`` field -> legacy path, published as before.
    * ``observed`` provenance -> the curve must match a *certified*
      snapshot; otherwise it is a half table and the asset is frozen.
    * ``absent`` provenance -> no curve may be published without evidence.
    * Anything else -> untrusted, frozen.
    """
    if event.get("kind") != "asset":
        return {"provenance": PROVENANCE_ATTESTED, "blocked": False,
                "reason": "not_an_asset"}
    prov = event.get("rating_provenance")
    if prov is None:
        return {"provenance": PROVENANCE_ATTESTED, "blocked": False,
                "reason": "legacy_asset_without_provenance"}
    if not isinstance(prov, dict):
        return {"provenance": PROVENANCE_UNTRUSTED, "blocked": True,
                "reason": "malformed_provenance"}
    status = prov.get("status")
    if status == "absent":
        return {"provenance": PROVENANCE_ABSENT, "blocked": True,
                "reason": "no_vendor_evidence"}
    if status != "observed":
        return {"provenance": PROVENANCE_UNTRUSTED, "blocked": True,
                "reason": f"unknown_provenance_status:{status}"}
    snapshot_id = prov.get("snapshot_id")
    if not snapshot_id:
        return {"provenance": PROVENANCE_UNTRUSTED, "blocked": True,
                "reason": "missing_snapshot_id"}
    snapshot = db.execute(
        "SELECT * FROM vendor_snapshot WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    if snapshot is None:
        return {"provenance": PROVENANCE_UNCERTIFIED, "blocked": True,
                "reason": "snapshot_not_captured", "snapshot_id": snapshot_id}
    from .extract import snapshot_rows
    try:
        rows = snapshot_rows(db, snapshot_id)
    except ValueError as error:
        return {"provenance": PROVENANCE_UNCERTIFIED, "blocked": True,
                "reason": str(error), "snapshot_id": snapshot_id}
    if rows != event["crane"].get("rating_curve"):
        return {"provenance": PROVENANCE_UNCERTIFIED, "blocked": True,
                "reason": "curve_differs_from_certified_snapshot",
                "snapshot_id": snapshot_id}
    published = db.execute(
        "SELECT 1 FROM asset_rating_curve WHERE snapshot_id=? "
        "AND crane_id=?", (snapshot_id, event["crane"].get("id"))).fetchone()
    if not published:
        return {"provenance": PROVENANCE_UNCERTIFIED, "blocked": True,
                "reason": "snapshot_not_published", "snapshot_id": snapshot_id}
    kind = snapshot["evidence_kind"]
    if kind == "archive":
        return {"provenance": PROVENANCE_HISTORICAL, "blocked": False,
                "reason": "archived_certified_snapshot",
                "snapshot_id": snapshot_id, "revision": snapshot["revision"]}
    return {"provenance": PROVENANCE_ATTESTED, "blocked": False,
            "reason": "certified_snapshot", "snapshot_id": snapshot_id,
            "revision": snapshot["revision"]}


def provenance(db, job_id):
    """Trust posture for a job's latest asset import.

    ``blocked`` means the curve must not release work: it was not backed
    by a certified, same-revision vendor snapshot.
    """
    row = db.execute(
        "SELECT * FROM asset_import WHERE job_id=? "
        "ORDER BY rowid DESC LIMIT 1", (job_id,)).fetchone()
    if row is None:
        return {"provenance": PROVENANCE_ABSENT, "blocked": False,
                "reason": "no_asset_event"}
    return {"provenance": row["rating_provenance"],
            "blocked": row["verdict"] == "blocked", "reason": row["reason"],
            "sha": row["sha"]}


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
            if kind=='asset':
                verdict=provenance_verdict(db,event)
                db.execute('INSERT OR REPLACE INTO asset_import(event_id,sha,'
                           'job_id,rating_provenance,verdict,reason) '
                           'VALUES(?,?,?,?,?,?)',
                           (event['id'],sha,event.get('job_id'),
                            verdict['provenance'],
                            'blocked' if verdict['blocked'] else 'ok',
                            verdict.get('reason','')))
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
    decisions,states=rules.calculate(events,db)
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
    """Replay source bytes into an empty database and compare conclusions.

    ``vendor_evidence`` replays the immutable vendor captures and
    publications (which are not ``source_files`` rows) into memory so their
    digest can be compared too.  It is only passed through ``audit``; the
    generic replay helper ignores it.
    """
    before=report(db)
    source_rows=list(db.execute('SELECT kind,raw FROM source_files ORDER BY rowid'))
    memory=connect(':memory:')
    # Vendor evidence must exist before the events are replayed, because an
    # asset event's provenance verdict is evaluated at import time.
    _copy_vendor_evidence(db,memory)
    for kind,raw in source_rows:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw); path=tmp.name
        try: import_file(memory,kind,path)
        finally: Path(path).unlink()
    after=report(memory)
    ok=before==after and _vendor_evidence(memory)==_vendor_evidence(db)
    memory.close()
    return {'ok':ok,'source_count':len(source_rows),'decision_count':len(before['decisions'])}


def _copy_vendor_evidence(source,target):
    tables={
        'vendor_capture':('portal_id','doc_id','revision','page_no','sha256',
                          'etag','signature','raw','fetched_at','discovered_at'),
        'vendor_snapshot':('snapshot_id','portal_id','doc_id','revision',
                           'expected_pages','captured_pages','state','gaps',
                           'coverage','raw_sha256','created_at','discovered_at',
                           'evidence_kind','current'),
        'vendor_snapshot_page':('snapshot_id','page_no','sha256','signature','etag'),
        'asset_rating_curve':('crane_id','revision','snapshot_id','rows',
                              'coverage','evidence','published_at'),
        'asset_import':('event_id','sha','job_id','rating_provenance','verdict',
                        'reason','imported_at'),
    }
    for table,columns in tables.items():
        sql='SELECT %s FROM %s'%(",".join(columns),table)
        placeholders=",".join("?"*len(columns))
        for row in source.execute(sql).fetchall():
            target.execute('INSERT OR IGNORE INTO %s VALUES(%s)'
                           %(table,placeholders),tuple(row))


def _vendor_evidence(db):
    """Materialised vendor/provenance digest used by :func:`audit`.

    The business ``report`` is intentionally blind to the captured vendor
    bytes (re-importing an event id would collide).  This digest ties the
    audit to the immutable page captures, snapshots and published curves so
    tampering with any of them is detectable, while an empty database
    replays to the same empty digest.
    """
    entries=[]
    for row in db.execute('SELECT snapshot_id,revision,state,gaps,coverage,'
                          'raw_sha256 FROM vendor_snapshot ORDER BY snapshot_id'):
        entries.append(['snapshot',tuple(row)])
    for row in db.execute('SELECT snapshot_id,page_no,sha256,signature,etag '
                          'FROM vendor_snapshot_page ORDER BY snapshot_id,page_no'):
        entries.append(['snapshot_page',tuple(row)])
    for row in db.execute('SELECT portal_id,doc_id,revision,page_no,sha256,etag,'
                          'signature FROM vendor_capture '
                          'ORDER BY portal_id,doc_id,revision,page_no,sha256'):
        entries.append(['capture',tuple(row)])
    for row in db.execute('SELECT crane_id,revision,snapshot_id,rows,coverage,'
                          'evidence,published_at FROM asset_rating_curve '
                          'ORDER BY crane_id,published_at,snapshot_id'):
        entries.append(['curve',tuple(row)])
    for row in db.execute('SELECT event_id,sha,job_id,rating_provenance,verdict '
                          'FROM asset_import ORDER BY event_id'):
        entries.append(['asset_import',tuple(row)])
    return hashlib.sha256(json.dumps(entries,ensure_ascii=False,
                                     sort_keys=True).encode()).hexdigest()
