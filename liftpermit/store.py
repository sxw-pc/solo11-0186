import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from . import plans, rules, tickets

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (sha TEXT PRIMARY KEY, kind TEXT NOT NULL, raw BLOB NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, sha TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (name TEXT PRIMARY KEY, result TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entity_state (subsystem TEXT NOT NULL, entity_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, PRIMARY KEY(subsystem,entity_id));
CREATE TABLE IF NOT EXISTS decision_history (revision INTEGER NOT NULL, name TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(revision,name));
CREATE TABLE IF NOT EXISTS business_record (subsystem TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_event TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(subsystem,entity_type,entity_id,version));
CREATE TABLE IF NOT EXISTS ticket_images (sha TEXT PRIMARY KEY, archive_path TEXT NOT NULL, bytes_n INTEGER NOT NULL, archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

# sqlite3.Connection 不允许挂任意属性，归档目录按连接登记。
_ARCHIVE_DIRS = {}


def connect(path, archive_dir=None):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if archive_dir is None and str(path) not in (":memory:", ""):
        archive_dir = str(Path(path).resolve().with_name(Path(path).name + ".archive"))
    _ARCHIVE_DIRS[id(db)] = archive_dir
    return db


def _body(event):
    return json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def archive_image(db, raw):
    """内容寻址归档一张票图。图像不是数据库事务的一部分：先落盘，再写事件。

    落盘失败时没有任何数据库写入；落盘成功而后续数据库事务失败时，图像保留，
    重跑按同一 sha 命中既有归档并把数据库重建到同一业务结论。绝不用一个
    “包住 OCR 和 DB 的事务”假装图像归档可回滚。
    """
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("image raw bytes required")
    sha = hashlib.sha256(bytes(raw)).hexdigest()
    existing = db.execute(
        "SELECT archive_path,bytes_n FROM ticket_images WHERE sha=?", (sha,)).fetchone()
    if existing:
        return sha, existing[0], existing[1]
    archive_dir = _ARCHIVE_DIRS.get(id(db))
    if not archive_dir:
        raise ValueError("ticket capture requires an explicit archive_dir")
    root = Path(archive_dir)
    target = root / sha[:2] / sha
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            tmp.write(bytes(raw))
            staged = Path(tmp.name)
        os.replace(staged, target)
    # 登记行由调用方在数据库事务内写入，确保事务回滚不留下孤儿登记；
    # 图像文件本身保留在归档中，重跑按 sha 幂等命中。
    return sha, str(target), len(raw)


def register_archived_image(db, raw):
    """门岗采录前置步骤：落盘并立即登记一张票图（独立提交）。

    与导入事务分离是有意的：图像归档不可被数据库事务回滚。后续票信封只引用
    返回的内容寻址 sha；若票信封事务失败，图像与登记保留，重跑幂等。
    """
    sha, archive_path, bytes_n = archive_image(db, raw)
    db.execute("INSERT OR IGNORE INTO ticket_images(sha,archive_path,bytes_n) VALUES (?,?,?)",
               (sha, archive_path, bytes_n))
    db.commit()
    return sha


def import_file(db, kind, file):
    raw = Path(file).read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, list) or not payload:
        raise ValueError('source must contain a nonempty event list')
    if kind == 'ticket':
        # 规范化在源 sha 之前：预归档信封只带 image_sha 时，从内容寻址归档
        # 回填原始字节，使 source_files 留存的字节自足，审计空库可逐字节重放。
        payload = _normalize_ticket_envelopes(db, payload)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    if db.execute('SELECT 1 FROM source_files WHERE sha=?', (sha,)).fetchone():
        return {'sha': sha, 'inserted': 0, 'idempotent': True}
    if kind == 'ticket':
        inserted = _ingest_ticket_payload(db, payload, sha)
    else:
        inserted = _ingest_standard_payload(db, payload, sha, kind)
    db.execute('INSERT INTO source_files(sha,kind,raw) VALUES (?,?,?)', (sha, kind, raw))
    rebuild(db)
    db.commit()
    return {'sha': sha, 'inserted': inserted, 'idempotent': False}


def _normalize_ticket_envelopes(db, payload):
    normalized = []
    for envelope in payload:
        if envelope.get('type') != 'capture':
            normalized.append(envelope)
            continue
        if 'image_base64' in envelope:
            normalized.append(envelope)
            continue
        image_sha = envelope.get('image_sha')
        if not image_sha:
            raise ValueError('capture requires image_base64 or archived image_sha')
        row = db.execute('SELECT archive_path FROM ticket_images WHERE sha=?',
                         (image_sha,)).fetchone()
        if not row:
            raise ValueError('capture references unarchived image_sha')
        env = dict(envelope)
        env['image_base64'] = base64.b64encode(Path(row[0]).read_bytes()).decode('ascii')
        normalized.append(env)
    return normalized


def _ingest_standard_payload(db, payload, sha, kind):
    db.execute('SAVEPOINT ingest')
    try:
        seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        for event in payload:
            if event.get('kind') != kind or not event.get('id'):
                raise ValueError('event kind/id mismatch')
            body = _body(event)
            old = db.execute('SELECT body FROM events WHERE event_id=?', (event['id'],)).fetchone()
            if old:
                if old[0] != body:
                    raise ValueError('same event id with different content')
                continue
            seq += 1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)', (event['id'], kind, body, sha, seq))
        db.execute('RELEASE SAVEPOINT ingest')
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT ingest')
        db.execute('RELEASE SAVEPOINT ingest')
        db.rollback()
        raise
    return len(payload)


def _ingest_ticket_payload(db, payload, sha):
    # 阶段一：信封结构校验 + 图像归档（落盘），全部发生在数据库写事务之前。
    # source_files 保留带原始图像字节的信封（审计可逐字节重放），事件体只留 sha。
    prepared = []
    image_registrations = []
    for envelope in payload:
        etype = envelope.get('type')
        if etype == 'capture':
            if 'image_base64' in envelope:
                image_sha, archive_path, bytes_n = archive_image(
                    db, base64.b64decode(envelope['image_base64'], validate=True))
                if not db.execute('SELECT 1 FROM ticket_images WHERE sha=?',
                                  (image_sha,)).fetchone():
                    image_registrations.append((image_sha, archive_path, bytes_n))
                envelope = {k: v for k, v in envelope.items() if k != 'image_base64'}
                envelope['image_sha'] = image_sha
            elif envelope.get('image_sha'):
                if not db.execute('SELECT 1 FROM ticket_images WHERE sha=?',
                                  (envelope['image_sha'],)).fetchone():
                    raise ValueError('capture references unarchived image_sha')
                archive_path = db.execute(
                    'SELECT archive_path FROM ticket_images WHERE sha=?',
                    (envelope['image_sha'],)).fetchone()[0]
                raw_image = Path(archive_path).read_bytes()
                envelope = dict(envelope)
                envelope['image_base64'] = base64.b64encode(raw_image).decode('ascii')
                # build_capture_event 会消费 base64 并只在事件里保留 sha
            else:
                raise ValueError('capture requires image_base64 or archived image_sha')
            prepared.append(tickets.build_capture_event(envelope))
        elif etype in ('ocr_rerun', 'manual_revision', 'correction', 'attribution'):
            prepared.append(tickets.build_derived_event(envelope))
        elif etype in ('promotion', 'countersign'):
            prepared.append(envelope)
        else:
            raise ValueError(f'unsupported ticket envelope type: {etype}')

    db.execute('SAVEPOINT ingest')
    try:
        for image_sha, archive_path, bytes_n in image_registrations:
            db.execute('INSERT INTO ticket_images(sha,archive_path,bytes_n) VALUES (?,?,?)',
                       (image_sha, archive_path, bytes_n))
        seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        inserted = 0

        def store_event(event):
            nonlocal seq, inserted
            body = _body(event)
            old = db.execute('SELECT body FROM events WHERE event_id=?',
                             (event['id'],)).fetchone()
            if old:
                if old[0] != body:
                    raise ValueError('same event id with different content')
                return
            seq += 1
            db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                       (event['id'], event['kind'], body, sha, seq))
            inserted += 1

        for event in prepared:
            if event.get('kind') == 'ticket':
                store_event(event)

        # 阶段二：推广/复签指令在同一可重放边界内把票域证据转成方案账新版本。
        for envelope in prepared:
            if envelope.get('type') not in ('promotion', 'countersign'):
                continue
            all_events = [json.loads(r[0]) for r in
                          db.execute('SELECT body FROM events ORDER BY seq')]
            view = tickets.evaluate([e for e in all_events if e.get('kind') == 'ticket'])
            directive_id = envelope.get('id')
            if not directive_id:
                raise ValueError('promotion/countersign requires explicit id')
            if envelope['type'] == 'promotion':
                plan_event = plans.build_ticket_plan_event(
                    all_events, view, envelope['job_id'],
                    effective_at=envelope['effective_at'], event_id=directive_id)
            else:
                plan_event = plans.build_countersign_event(
                    all_events, envelope['job_id'],
                    effective_at=envelope['effective_at'],
                    signer=envelope['signer'], event_id=directive_id)
            store_event(plan_event)

        # 同图不同 OCR 等领域冲突在提交前即可被 evaluate 确定性地暴露。
        all_events = [json.loads(r[0]) for r in
                      db.execute('SELECT body FROM events ORDER BY seq')]
        tickets.evaluate([e for e in all_events if e.get('kind') == 'ticket'])
        db.execute('RELEASE SAVEPOINT ingest')
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT ingest')
        db.execute('RELEASE SAVEPOINT ingest')
        db.rollback()
        raise
    return inserted


def rebuild(db):
    events = [json.loads(row[0]) for row in db.execute('SELECT body FROM events ORDER BY seq')]
    decisions, states = rules.calculate(events)
    revision = db.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] + 1
    db.execute('DELETE FROM decisions')
    db.execute('DELETE FROM entity_state')
    db.execute('DELETE FROM business_record')
    for name, item in sorted(decisions.items()):
        db.execute('INSERT INTO decisions VALUES (?,?,?,?)', (name, item['result'], item['reason'], json.dumps(item['evidence'], ensure_ascii=False, sort_keys=True)))
        db.execute('INSERT OR REPLACE INTO decision_history VALUES (?,?,?,?)', (revision, name, item['result'], item['reason']))
    for subsystem, entity, state, version in states:
        db.execute('INSERT INTO entity_state VALUES (?,?,?,?)', (subsystem, entity, state, version))
    for event in events:
        for subsystem, entity_type, entity_id, body in rules.business_records(event):
            db.execute('INSERT OR REPLACE INTO business_record VALUES (?,?,?,?,?,?)', (subsystem, entity_type, entity_id, event['id'], event.get('version', 1), json.dumps(body, ensure_ascii=False, sort_keys=True)))


def ticket_view(db):
    events = [json.loads(row[0]) for row in db.execute(
        "SELECT body FROM events WHERE kind='ticket' ORDER BY seq")]
    return tickets.evaluate(events)


def report(db):
    return {'decisions': {r['name']: {'result': r['result'], 'reason': r['reason'], 'evidence': json.loads(r['evidence'])} for r in db.execute('SELECT * FROM decisions ORDER BY name')},
            'entities': [dict(r) for r in db.execute('SELECT * FROM entity_state ORDER BY subsystem,entity_id')],
            'business_records': [dict(r) for r in db.execute('SELECT * FROM business_record ORDER BY subsystem,entity_type,entity_id,version')],
            'ticket_images': [dict(r) for r in db.execute('SELECT sha,archive_path,bytes_n FROM ticket_images ORDER BY sha')],
            'ticket_view': ticket_view(db),
            'sources': [dict(r) for r in db.execute('SELECT sha,kind FROM source_files ORDER BY sha')]}


def audit(db):
    before = report(db)
    source_rows = list(db.execute('SELECT kind,raw FROM source_files ORDER BY rowid'))
    replay_archive = tempfile.mkdtemp(prefix='ticket-audit-')
    memory = connect(':memory:', archive_dir=replay_archive)
    for kind, raw in source_rows:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(raw)
            path = tmp.name
        try:
            import_file(memory, kind, path)
        finally:
            Path(path).unlink()
    after = report(memory)
    memory.close()
    # 归档物理路径随环境变化，审计只比对内容寻址 sha 与字节数；
    # 图像原始字节在 source_files 的 ticket 信封里可逐字节重放。
    for snapshot in (before, after):
        snapshot['ticket_images'] = [
            {'sha': r['sha'], 'bytes_n': r['bytes_n']}
            for r in snapshot['ticket_images']]
    return {'ok': before == after, 'source_count': len(source_rows),
            'decision_count': len(before['decisions'])}
