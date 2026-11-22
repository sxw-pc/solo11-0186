"""Capture local mock supplier pages for a crane rating table."""
import hashlib
import json
from pathlib import Path
from .store import connect


def import_page(db_path,file):
    raw=Path(file).read_bytes()
    doc=json.loads(raw)
    if not doc['doc_id'] or doc['page_no']<1 or not isinstance(doc['rows'],list):
        raise ValueError('invalid_vendor_page')
    sha=hashlib.sha256(raw).hexdigest()
    db=connect(db_path)
    try:
        with db:
            db.execute('INSERT OR IGNORE INTO vendor_page VALUES(?,?,?,?,?)',
                       (sha,doc['doc_id'],doc['page_no'],doc['revision'],raw))
            from .extract import rating_rows
            return {'sha':sha,'rows':rating_rows(db,doc['doc_id'])}
    finally:db.close()
