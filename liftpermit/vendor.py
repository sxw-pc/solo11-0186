"""Revision-bound vendor capture, certification, and asset publication.

Design invariants
-----------------
* Pages are stored as immutable rows keyed by
  ``(portal_id, doc_id, revision, page_no, sha256)``.  Nothing is ever
  overwritten, so two revisions can coexist and an old capture is never
  rewritten into a new one.
* A snapshot is a *set* of page rows pinned to one revision.  It is only
  ``certified`` when: every expected page of that revision is present,
  each page byte-hash matches both the captured bytes and the vendor
  signature, and the pages jointly cover the plan's applicable radius
  and boom.
* A partial capture produces a ``pending`` snapshot that exposes the
  literal raw pages captured so far plus a ``gaps`` list.  It never
  yields a merged rating curve.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import portal as portal_module
from .store import connect

PENDING = "pending"
CERTIFIED = "certified"
AMBIGUOUS = "ambiguous"

# Retry policy for a *fixed* revision.  A page that keeps failing after
# ``max_attempts_per_page`` contributes an explicit gap; the scan makes at
# most ``max_passes`` passes before reporting ``converged: False`` and
# leaving the snapshot ``pending``/``ambiguous``.
DEFAULT_RETRY_POLICY = {
    "max_attempts_per_page": 3,
    "max_passes": 4,
    "backoff_seconds": 0,
}


class CaptureError(Exception):
    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def import_page(db_path, file):
    """Legacy provisional capture kept for the seeded fixtures.

    Writes a raw page and returns the *provisional* merge produced by
    :func:`liftpermit.extract.rating_rows`.  The merge is not authoritative
    and is not what :func:`publish_snapshot` uses.
    """
    raw = Path(file).read_bytes()
    doc = json.loads(raw)
    if not doc.get("doc_id") or int(doc.get("page_no", 0)) < 1 \
            or not isinstance(doc.get("rows"), list):
        raise ValueError("invalid_vendor_page")
    sha = _sha256(raw)
    db = connect(db_path)
    try:
        with db:
            db.execute(
                "INSERT OR IGNORE INTO vendor_page VALUES(?,?,?,?,?)",
                (sha, doc["doc_id"], doc["page_no"], doc["revision"], raw))
            from .extract import rating_rows
            return {"sha": sha, "rows": rating_rows(db, doc["doc_id"])}
    finally:
        db.close()


class VendorCollector:
    """Collects page evidence from one portal into one SQLite database."""

    def __init__(self, db, portal, retry_policy=None, doc_id=None,
                 sleep=None):
        self.db = db if isinstance(db, object) and hasattr(db, "execute") \
            else connect(db)
        self.portal = portal
        self.policy = dict(DEFAULT_RETRY_POLICY)
        if retry_policy:
            self.policy.update(retry_policy)
        self.doc_id = doc_id
        # Injected so a test can observe back-off without real sleeping.
        self.sleep = sleep or (lambda seconds: None)

    # ------------------------------------------------------------- discovery
    def _resolve_doc(self, doc_id):
        doc_id = doc_id or self.doc_id
        if doc_id is None:
            docs = list(self.portal.documents)
            if len(docs) != 1:
                raise CaptureError("doc_id_required", documents=docs)
            doc_id = docs[0]
        return doc_id

    def discover(self, doc_id=None):
        doc_id = self._resolve_doc(doc_id)
        return self.portal.discover(doc_id)

    # --------------------------------------------------------------- capture
    def _load_cached(self, doc_id, revision, page_no):
        row = self.db.execute(
            "SELECT * FROM vendor_capture WHERE portal_id=? AND doc_id=? "
            "AND revision=? AND page_no=? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
            (self.portal.portal_id, doc_id, revision, page_no)).fetchone()
        return row

    def _store_page(self, doc_id, revision, page_no, raw, etag, signature,
                    discovered_at):
        digest = _sha256(raw)
        fetched_at = self.portal.clock.iso()
        self.db.execute(
            "INSERT OR IGNORE INTO vendor_capture(portal_id,doc_id,revision,page_no,"
            "sha256,etag,signature,raw,fetched_at,discovered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.portal.portal_id, doc_id, revision, page_no, digest, etag,
             signature, raw, fetched_at, discovered_at))
        return digest

    def _capture_one(self, doc_id, revision, entry, lease_id, resume):
        """Fetch one page with bounded retries.

        Returns ``(status, record, error)`` where status is one of
        ``fetched``, ``not_modified``, ``failed`` or ``gone``.
        """
        page_no = entry["page_no"]
        attempts = 0
        last_error = None
        cached = self._load_cached(doc_id, revision, page_no) if resume else None
        etag = cached["etag"] if cached else None
        while attempts < self.policy["max_attempts_per_page"]:
            attempts += 1
            try:
                response = self.portal.fetch_page(entry["url"], etag=etag,
                                                  lease_id=lease_id)
            except portal_module.PortalTimeout:
                last_error = {"code": "timeout", "attempts": attempts}
                self.sleep(self.policy["backoff_seconds"])
                continue
            if response.status == 200:
                raw = response.body
                digest = _sha256(raw)
                header_digest = response.headers.get("x-page-sha256")
                signature = response.headers.get("x-page-signature", "")
                served_revision = response.headers.get("x-revision")
                if served_revision and served_revision != revision:
                    return "failed", None, {
                        "code": "revision_changed_midflight",
                        "expected": revision, "served": served_revision,
                        "page_no": page_no, "attempts": attempts}
                if header_digest and header_digest != digest:
                    return "failed", None, {
                        "code": "page_digest_mismatch", "page_no": page_no,
                        "header": header_digest, "body": digest,
                        "attempts": attempts}
                if not self.portal.verify_page_signature(
                        doc_id, revision, page_no, digest, signature):
                    return "failed", None, {
                        "code": "page_signature_invalid", "page_no": page_no,
                        "attempts": attempts}
                stored = self._store_page(
                    doc_id, revision, page_no, raw,
                    response.headers.get("etag"), signature,
                    entry.get("discovered_at", self.portal.clock.iso()))
                return "fetched", {
                    "page_no": page_no, "sha256": stored,
                    "etag": response.headers.get("etag"),
                    "signature": signature,
                    "duplicate": response.headers.get("x-duplicate-delivery"),
                    "attempts": attempts,
                }, None
            if response.status == 304:
                if (response.error or {}).get("code") == "stale_not_modified":
                    return "failed", None, {
                        "code": "stale_etag", "page_no": page_no,
                        "served_revision": (response.error or {}).get(
                            "served_revision"),
                        "attempts": attempts}
                if not cached:
                    return "failed", None, {
                        "code": "unsolicited_304", "page_no": page_no}
                if cached["etag"] != etag:
                    return "failed", None, {
                        "code": "stale_etag", "page_no": page_no}
                return "not_modified", {
                    "page_no": page_no, "sha256": cached["sha256"],
                    "etag": cached["etag"], "signature": cached["signature"],
                    "reused_cache": True, "attempts": attempts,
                }, None
            if response.status == 410:
                return "gone", None, {"code": "page_gone", "page_no": page_no,
                                      "attempts": attempts}
            # 4xx other than 410 are terminal; 5xx are retried.
            code = (response.error or {}).get("code", f"http_{response.status}")
            last_error = {"code": code, "status": response.status,
                          "page_no": page_no, "attempts": attempts}
            if 400 <= response.status < 500:
                return "failed", None, last_error
            self.sleep(self.policy["backoff_seconds"])
        return "failed", None, last_error or {"code": "unknown", "page_no": page_no}

    # --------------------------------------------------------------- snapshot
    def capture(self, doc_id=None, revision=None, lease_id=None, resume=True,
                applicable=None, request_lease=True, lease_ttl=None):
        """Capture every page of one revision and build a snapshot.

        ``revision`` defaults to the revision the manifest advertises at
        discovery time.  The snapshot is bound to that revision even if the
        portal flips to another revision during the scan; a mid-scan flip
        is reported as a ``revision_drift`` gap instead of being merged.

        When the revision is served under a read lease (the current V2),
        the collector obtains one automatically unless ``request_lease``
        is false or a ``lease_id`` is supplied.
        """
        doc_id = self._resolve_doc(doc_id)
        discovered_at = self.portal.clock.iso()
        manifest = self.portal.discover(doc_id)
        revision = revision or manifest["revision"]
        if revision != manifest["revision"]:
            # Explicitly requested historical/in-flight revision: re-read the
            # manifest pinned to that revision when the portal can serve it.
            raise CaptureError("revision_not_current",
                               requested=revision, current=manifest["revision"])
        expected = manifest["total_pages"]
        entries = {entry["page_no"]: entry for entry in manifest["pages"]}

        if request_lease and lease_id is None:
            lease_cfg = self.portal.revision_config(doc_id, revision).get("lease")
            if lease_cfg:
                lease = self.portal.request_lease(doc_id, ttl_seconds=lease_ttl)
                lease_id = lease["lease_id"]
                lease_record = lease
            else:
                lease_record = None
        else:
            lease_record = None

        pages, gaps, failures, duplicates = [], [], [], []
        terminal = set()
        converged = False
        passes = 0
        drift = False
        while passes < self.policy["max_passes"]:
            passes += 1
            missing = [n for n in range(1, expected + 1)
                       if n not in {p["page_no"] for p in pages}
                       and n not in terminal]
            if not missing:
                converged = True
                break
            progressed = False
            for page_no in missing:
                entry = entries.get(page_no)
                if entry is None:
                    # The document listing may have grown/shrank between
                    # discovery and this pass; re-discover before giving up.
                    refreshed = self.portal.discover(doc_id)
                    if refreshed["revision"] != revision:
                        drift = True
                        gaps.append({"page_no": page_no,
                                     "code": "revision_changed_midscan",
                                     "served": refreshed["revision"]})
                        break
                    expected = max(expected, refreshed["total_pages"])
                    entries = {e["page_no"]: e for e in refreshed["pages"]}
                    entry = entries.get(page_no)
                    if entry is None:
                        gaps.append({"page_no": page_no,
                                     "code": "missing_link"})
                        terminal.add(page_no)
                        continue
                entry = dict(entry, discovered_at=discovered_at)
                status, record, error = self._capture_one(
                    doc_id, revision, entry, lease_id, resume)
                if status in ("fetched", "not_modified"):
                    pages.append(record)
                    progressed = True
                    if record.get("duplicate"):
                        duplicates.append({"page_no": page_no,
                                           "delivery": record["duplicate"]})
                elif status == "gone":
                    gaps.append({"page_no": page_no, "code": "page_gone"})
                    # 410 is a terminal answer for this revision.
                    terminal.add(page_no)
                else:
                    failures.append(error)
                    if (error or {}).get("code") == "revision_changed_midflight":
                        drift = True
                    elif (error or {}).get("code") in (
                            "page_digest_mismatch", "page_signature_invalid",
                            "unsolicited_304", "stale_etag"):
                        terminal.add(page_no)
            if drift:
                break
            # A pass that fetched nothing new cannot converge by retrying.
            if not progressed:
                break

        pages.sort(key=lambda p: p["page_no"])
        missing_pages = [n for n in range(1, expected + 1)
                         if n not in {p["page_no"] for p in pages}]
        for page_no in missing_pages:
            if not any(g["page_no"] == page_no for g in gaps):
                gaps.append({"page_no": page_no, "code": "not_captured"})

        coverage = self._coverage(doc_id, revision, pages, expected, applicable)
        # Re-read the current revision; a flip means the capture must be
        # treated as non-authoritative for publication.
        post = self.portal.discover(doc_id)["revision"]
        drift = drift or post != revision
        state = self._state(expected, pages, gaps, coverage, drift, applicable)

        snapshot_id = self._persist_snapshot(
            doc_id, revision, expected, pages, gaps, duplicates, failures,
            coverage, state, drift, discovered_at)
        return {
            "snapshot_id": snapshot_id,
            "doc_id": doc_id,
            "revision": revision,
            "expected_pages": expected,
            "captured_pages": [p["page_no"] for p in pages],
            "missing_pages": missing_pages,
            "gaps": gaps,
            "failures": failures,
            "duplicates": duplicates,
            "coverage": coverage,
            "applicable": applicable,
            "state": state,
            "certified": state == CERTIFIED,
            "undecidable": state == AMBIGUOUS,
            "revision_drift": drift,
            "converged": converged,
            "passes": passes,
            "lease_id": lease_id,
            "lease": lease_record,
            "pages": pages,
            "raw_pages": self._raw_pages(snapshot_id),
            "rows": (self._snapshot_rows(snapshot_id) if state == CERTIFIED
                     else None),
        }

    # ------------------------------------------------------------- coverage
    def _coverage(self, doc_id, revision, pages, expected, applicable):
        present = {p["page_no"] for p in pages}
        missing = [n for n in range(1, expected + 1) if n not in present]
        required = [tuple(cell) for cell in self.portal.required_coverage(doc_id)]
        seen = set()
        for page in pages:
            row = self.db.execute(
                "SELECT raw FROM vendor_capture WHERE portal_id=? AND doc_id=? "
                "AND revision=? AND page_no=? AND sha256=? LIMIT 1",
                (self.portal.portal_id, doc_id, revision, page["page_no"],
                 page["sha256"])).fetchone()
            if row:
                for cell in json.loads(row["raw"])["rows"]:
                    seen.add((cell["radius_m"], cell["boom_m"]))
        missing_cells = [list(cell) for cell in required if cell not in seen]
        result = {
            "complete": bool(expected) and not missing,
            "missing_pages": missing,
            "required_radius_boom": [list(cell) for cell in required],
            "uncovered_radius_boom": missing_cells,
            "applicable_radius_boom": [list(cell) for cell in required],
        }
        if applicable:
            want = (int(applicable["radius_m"]), int(applicable["boom_m"]))
            result["applicable_radius_boom"] = [list(want)]
            result["applicable_covered"] = want in seen
        return result

    def _state(self, expected, pages, gaps, coverage, drift, applicable):
        if drift:
            return AMBIGUOUS
        if gaps or len(pages) != expected:
            return PENDING
        if not coverage["complete"]:
            return PENDING
        if applicable and not coverage.get("applicable_covered"):
            return PENDING
        return CERTIFIED

    # ------------------------------------------------------------ persistence
    def _persist_snapshot(self, doc_id, revision, expected, pages, gaps,
                          duplicates, failures, coverage, state, drift,
                          discovered_at):
        doc = {
            "portal_id": self.portal.portal_id, "doc_id": doc_id,
            "revision": revision, "expected_pages": expected,
            "pages": pages, "gaps": gaps, "duplicates": duplicates,
            "failures": failures, "coverage": coverage,
            "revision_drift": drift,
        }
        digest = _sha256(_canonical(doc))
        snapshot_id = f"{doc_id}/{revision}/{digest[:16]}"
        self.db.execute("SAVEPOINT snapshot")
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO vendor_snapshot(snapshot_id,portal_id,doc_id,"
                "revision,expected_pages,captured_pages,state,gaps,coverage,raw_sha256,"
                "created_at,discovered_at,evidence_kind,current) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, self.portal.portal_id, doc_id, revision, expected,
                 len(pages), state,
                 json.dumps(gaps, ensure_ascii=False, sort_keys=True),
                 json.dumps(coverage, ensure_ascii=False, sort_keys=True),
                 digest, self.portal.clock.iso(), discovered_at, "scan",
                 1 if not drift else 0))
            self.db.execute(
                "DELETE FROM vendor_snapshot_page WHERE snapshot_id=?",
                (snapshot_id,))
            for page in pages:
                self.db.execute(
                    "INSERT INTO vendor_snapshot_page(snapshot_id,page_no,sha256,"
                    "signature,etag) VALUES(?,?,?,?,?)",
                    (snapshot_id, page["page_no"], page["sha256"],
                     page.get("signature", ""), page.get("etag", "")))
            self.db.execute("RELEASE SAVEPOINT snapshot")
            self.db.commit()
        except Exception:
            self.db.execute("ROLLBACK TO SAVEPOINT snapshot")
            self.db.execute("RELEASE SAVEPOINT snapshot")
            self.db.rollback()
            raise
        return snapshot_id

    def _raw_pages(self, snapshot_id):
        snapshot = self.db.execute(
            "SELECT portal_id, doc_id, revision FROM vendor_snapshot "
            "WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        result = []
        if snapshot is None:
            return result
        for row in self.db.execute(
                "SELECT sp.page_no, vc.raw, sp.sha256 FROM vendor_snapshot_page sp "
                "JOIN vendor_capture vc ON vc.portal_id=? AND vc.doc_id=? "
                "AND vc.revision=? AND vc.page_no=sp.page_no "
                "AND vc.sha256=sp.sha256 "
                "WHERE sp.snapshot_id=? ORDER BY sp.page_no",
                (snapshot["portal_id"], snapshot["doc_id"],
                 snapshot["revision"], snapshot_id)):
            result.append({"page_no": row["page_no"], "sha256": row["sha256"],
                           "raw": bytes(row["raw"])})
        return result

    def _snapshot_rows(self, snapshot_id):
        from .extract import snapshot_rows
        return snapshot_rows(self.db, snapshot_id)


# ---------------------------------------------------------------- publishing
def publish_snapshot(db, snapshot_id, crane_id, job_id=None, now=None):
    """Publish a certified snapshot as an asset rating curve, or refuse.

    Returns a dict describing the publication.  A non-certified snapshot
    yields ``{"published": False, "missing_pages": [...], "raw_pages": [...]}``
    and never touches ``asset_rating_curve``.
    """
    snapshot = db.execute(
        "SELECT * FROM vendor_snapshot WHERE snapshot_id=?",
        (snapshot_id,)).fetchone()
    if snapshot is None:
        raise CaptureError("unknown_snapshot", snapshot_id=snapshot_id)
    raw_pages = []
    for row in db.execute(
            "SELECT sp.page_no, vc.raw, sp.sha256, sp.signature "
            "FROM vendor_snapshot_page sp JOIN vendor_capture vc "
            "ON vc.portal_id=? AND vc.doc_id=? AND vc.revision=? "
            "AND vc.page_no=sp.page_no AND vc.sha256=sp.sha256 "
            "WHERE sp.snapshot_id=? ORDER BY sp.page_no",
            (snapshot["portal_id"], snapshot["doc_id"], snapshot["revision"],
             snapshot_id)):
        raw_pages.append({"page_no": row["page_no"], "sha256": row["sha256"],
                          "signature": row["signature"],
                          "raw": bytes(row["raw"])})
    if snapshot["state"] != CERTIFIED:
        gaps = json.loads(snapshot["gaps"])
        missing = [g["page_no"] for g in gaps]
        return {"published": False, "state": snapshot["state"],
                "revision": snapshot["revision"], "snapshot_id": snapshot_id,
                "missing_pages": sorted(set(missing)),
                "raw_pages": raw_pages, "crane_id": crane_id,
                "job_id": job_id}
    # Re-prove page integrity immediately before writing the curve: every
    # stored page must still hash to its recorded digest and the page set must
    # still be exactly 1..expected.  A row edited after certification fails
    # here instead of being published.
    expected = int(snapshot["expected_pages"])
    by_page = {p["page_no"]: p for p in raw_pages}
    problems = []
    if sorted(by_page) != list(range(1, expected + 1)):
        problems.append({"code": "page_set_incomplete",
                         "pages": sorted(by_page)})
    for page_no, page in by_page.items():
        if _sha256(page["raw"]) != page["sha256"]:
            problems.append({"code": "page_digest_changed", "page_no": page_no})
    coverage = json.loads(snapshot["coverage"])
    if coverage.get("missing_pages") or not coverage.get("complete", False):
        problems.append({"code": "coverage_incomplete",
                         "missing": coverage.get("missing_pages")})
    if problems:
        return {"published": False, "state": "pending",
                "revision": snapshot["revision"], "snapshot_id": snapshot_id,
                "missing_pages": [p.get("page_no") for p in problems
                                  if p.get("page_no") is not None],
                "problems": problems, "raw_pages": raw_pages,
                "crane_id": crane_id, "job_id": job_id}
    from .extract import snapshot_rows
    rows = snapshot_rows(db, snapshot_id)
    evidence = {
        "portal_id": snapshot["portal_id"], "doc_id": snapshot["doc_id"],
        "revision": snapshot["revision"],
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot["raw_sha256"],
        "page_sha256": {str(p["page_no"]): p["sha256"] for p in raw_pages},
    }
    db.execute(
        "INSERT INTO asset_rating_curve(crane_id,revision,snapshot_id,rows,"
        "coverage,evidence,published_at) VALUES(?,?,?,?,?,?,?)",
        (crane_id, snapshot["revision"], snapshot_id,
         json.dumps(rows, ensure_ascii=False, sort_keys=True),
         snapshot["coverage"], json.dumps(evidence, ensure_ascii=False,
                                          sort_keys=True),
         now or snapshot["created_at"]))
    db.commit()
    return {"published": True, "state": "certified", "revision":
            snapshot["revision"], "snapshot_id": snapshot_id,
            "rows": rows, "evidence": evidence, "crane_id": crane_id,
            "job_id": job_id}


def published_curves(db, crane_id=None):
    sql = "SELECT * FROM asset_rating_curve"
    params = ()
    if crane_id:
        sql += " WHERE crane_id=?"
        params = (crane_id,)
    sql += " ORDER BY published_at, rowid"
    return [{**dict(row), "rows": json.loads(row["rows"]),
             "coverage": json.loads(row["coverage"]),
             "evidence": json.loads(row["evidence"])}
            for row in db.execute(sql, params)]


# -------------------------------------------------------------- certification
def verify_archive(db, portal, doc_id, revision):
    """Verify a monthly signed archive and return an archived snapshot.

    The archive is a closed-revision proof: it can substantiate a
    historical V1, and it is explicitly *not* the current revision.  Asking
    the portal for the current revision's archive is refused by the portal
    (``archive_not_published``) because that evidence does not exist; the
    caller must use a read lease for the live revision instead.
    """
    response = portal.fetch_archive(doc_id, revision)
    if response.status != 200:
        raise CaptureError((response.error or {}).get("code", "archive_unavailable"),
                           status=response.status,
                           detail=response.error,
                           doc_id=doc_id, revision=revision)
    import io
    import zipfile
    buffer = io.BytesIO(response.body)
    with zipfile.ZipFile(buffer) as zf:
        manifest_bytes = zf.read("manifest.json")
        signature = zf.read("signature.txt").decode()
        manifest = json.loads(manifest_bytes)
        packaged = {name: zf.read(name) for name in zf.namelist()
                    if name.startswith("pages/")}
    if not hmac_compare(portal.archive_signature(manifest_bytes), signature):
        raise CaptureError("archive_signature_invalid", doc_id=doc_id,
                           revision=revision)
    if manifest["doc_id"] != doc_id or manifest["revision"] != revision:
        raise CaptureError("archive_manifest_mismatch", doc_id=doc_id,
                           revision=revision)
    pages, gaps = [], []
    expected = int(manifest["total_pages"])
    entries = {int(e["page_no"]): e for e in manifest["pages"]}
    for page_no in range(1, expected + 1):
        entry = entries.get(page_no)
        raw = packaged.get(f"pages/page-{page_no}.json")
        if entry is None or raw is None:
            gaps.append({"page_no": page_no, "code": "archive_page_missing"})
            continue
        digest = _sha256(raw)
        if digest != entry["sha256"]:
            gaps.append({"page_no": page_no, "code": "archive_digest_mismatch"})
            continue
        if not portal.verify_page_signature(doc_id, revision, page_no, digest,
                                            entry["signature"]):
            gaps.append({"page_no": page_no, "code": "archive_signature_invalid"})
            continue
        pages.append({"page_no": page_no, "sha256": digest,
                      "signature": entry["signature"],
                      "etag": entry.get("etag", ""), "raw": raw,
                      "archive": manifest["archive_id"]})
    coverage = _archive_coverage(portal, doc_id, pages, expected)
    is_current = revision == portal.current_revision(doc_id)
    state = CERTIFIED if (not gaps and len(pages) == expected
                          and coverage["complete"] and not is_current) else PENDING
    gap_codes = gaps
    if is_current:
        gap_codes = gaps + [{"page_no": None, "code": "archive_not_current"}]
        state = PENDING
    snapshot_id = _persist_archive_snapshot(
        db, portal, doc_id, revision, expected, pages, gap_codes, coverage,
        manifest, state)
    return {
        "snapshot_id": snapshot_id, "doc_id": doc_id, "revision": revision,
        "state": state, "certified": state == CERTIFIED,
        "gaps": gap_codes, "coverage": coverage,
        "manifest_sha256": _sha256(manifest_bytes),
        "archive_id": manifest["archive_id"],
        "is_current_revision": is_current,
        "pages": [{k: v for k, v in p.items() if k != "raw"} for p in pages],
        "raw_pages": pages,
        "rows": ({p["page_no"]: _rows_of(p["raw"]) for p in pages}
                 if state == CERTIFIED else None),
    }


def hmac_compare(left, right):
    import hmac as _hmac
    return _hmac.compare_digest(str(left), str(right))


def _rows_of(raw):
    return json.loads(raw)["rows"]


def _archive_coverage(portal, doc_id, pages, expected):
    present = {p["page_no"] for p in pages}
    required = [tuple(c) for c in portal.required_coverage(doc_id)]
    seen = set()
    for page in pages:
        for cell in _rows_of(page["raw"]):
            seen.add((cell["radius_m"], cell["boom_m"]))
    return {
        "complete": bool(expected) and len(present) == expected,
        "missing_pages": [n for n in range(1, expected + 1) if n not in present],
        "required_radius_boom": [list(c) for c in required],
        "uncovered_radius_boom": [list(c) for c in required if c not in seen],
    }


def _persist_archive_snapshot(db, portal, doc_id, revision, expected, pages,
                              gaps, coverage, manifest, state):
    doc = {"portal_id": portal.portal_id, "doc_id": doc_id,
           "revision": revision, "archive_id": manifest["archive_id"],
           "expected_pages": expected, "pages": [
               {k: v for k, v in p.items() if k != "raw"} for p in pages],
           "gaps": gaps, "coverage": coverage}
    digest = _sha256(_canonical(doc))
    snapshot_id = f"{doc_id}/{revision}/archive-{digest[:16]}"
    for page in pages:
        db.execute(
            "INSERT OR IGNORE INTO vendor_capture(portal_id,doc_id,revision,"
            "page_no,sha256,etag,signature,raw,fetched_at,discovered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (portal.portal_id, doc_id, revision, page["page_no"],
             page["sha256"], page["etag"], page["signature"], page["raw"],
             portal.clock.iso(), manifest["issued_at"]))
    db.execute(
        "INSERT OR IGNORE INTO vendor_snapshot(snapshot_id,portal_id,doc_id,"
        "revision,expected_pages,captured_pages,state,gaps,coverage,raw_sha256,"
        "created_at,discovered_at,evidence_kind,current) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snapshot_id, portal.portal_id, doc_id, revision, expected,
         len(pages), state,
         json.dumps(gaps, ensure_ascii=False, sort_keys=True),
         json.dumps(coverage, ensure_ascii=False, sort_keys=True),
         digest, portal.clock.iso(), manifest["issued_at"], "archive", 0))
    for page in pages:
        db.execute(
            "INSERT OR REPLACE INTO vendor_snapshot_page(snapshot_id,page_no,"
            "sha256,signature,etag) VALUES(?,?,?,?,?)",
            (snapshot_id, page["page_no"], page["sha256"],
             page["signature"], page["etag"]))
    db.commit()
    return snapshot_id
