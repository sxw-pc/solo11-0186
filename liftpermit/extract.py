"""Baseline extraction merges all downloaded table rows by page number."""
import json


def rating_rows(db,doc_id):
    rows=[]
    for page in db.execute('SELECT raw FROM vendor_page WHERE doc_id=? ORDER BY page_no',
                           (doc_id,)):
        rows.extend(json.loads(page['raw'])['rows'])
    return rows
