"""Deterministic, offline mock of a tower-crane vendor document portal.

The portal is the *source* of the evidence: it owns page bytes, ETags,
revisions, signatures, the monthly signed archive, and time-bound read
leases.  Nothing here touches the network.  Tests and the night-shift
collector drive it in-process with a :class:`ManualClock`, which keeps
every failure mode (timeouts, 5xx, stale caches, revision flips, lease
expiry) reproducible.

Two vendor-recognised evidence protocols live here:

``archive``
    A monthly signed ZIP with the full page listing, per-page digests and
    release manifest for a *closed* revision.  It can prove a historical
    version (V1) but it can never be presented as the current revision.

``lease``
    A time-bound read lease for the *current* revision (V2).  Pages can
    only be read while the lease is valid.  It produces a fresh snapshot
    of the current revision and never back-fills the historical archive.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import zipfile
from datetime import datetime, timedelta


class PortalTimeout(Exception):
    """The mock portal drops the connection before producing a response."""


class PortalError(Exception):
    """Anything the portal refuses to serve, carrying a stable code."""

    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


class ManualClock:
    """A clock the test can stop, read, and advance deterministically."""

    def __init__(self, start="2026-12-01T08:00:00"):
        self._now = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S")

    def now(self):
        return self._now

    def iso(self):
        return self._now.strftime("%Y-%m-%dT%H:%M:%S")

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)
        return self.iso()

    def set(self, value):
        self._now = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        return self.iso()


class PortalResponse:
    def __init__(self, status, body=b"", headers=None, error=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.error = error

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f"PortalResponse(status={self.status}, headers={self.headers})"


class MockPortal:
    """A fixture-driven mock vendor portal.

    ``spec`` is the multi-version document fixture.  ``scenario`` injects
    transient faults; it is copied so a test can reset and replay the same
    scenario deterministically.
    """

    def __init__(self, spec, clock=None, scenario=None):
        if isinstance(spec, (str, bytes)):
            spec = json.loads(spec)
        self.spec = json.loads(json.dumps(spec))
        self.portal_id = self.spec["portal_id"]
        self.signing_key = self.spec.get("signing_key", "offline-mock-vendor-key")
        self.documents = self.spec["documents"]
        self.clock = clock or ManualClock()
        self.scenario = dict(scenario or {})
        self.current = {}
        self._leases = {}
        self._delivery = {}
        self._faults = {}
        self._discoveries = {}
        self._flips = 0
        self._fetch_count = 0
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self):
        self.current = {doc: cfg["current_revision"]
                        for doc, cfg in self.documents.items()}
        self._leases = {}
        self._delivery = {}
        self._faults = {key: list(items) if isinstance(items, list) else items
                        for key, items in self.scenario.get("page_faults", {}).items()}
        self._discoveries = {doc: list(counts)
                             for doc, counts in self.scenario.get("discovery_pages", {}).items()}
        self._flips = 0
        self._fetch_count = 0
        self.clock = self.clock or ManualClock()

    def set_scenario(self, scenario):
        self.scenario = dict(scenario or {})
        self.reset()

    def advance(self, seconds):
        return self.clock.advance(seconds)

    # ------------------------------------------------------------------ model
    def revision_config(self, doc_id, revision):
        try:
            return self.documents[doc_id]["revisions"][revision]
        except KeyError:
            raise PortalError("unknown_document_or_revision",
                              doc_id=doc_id, revision=revision)

    def current_revision(self, doc_id):
        return self.current[doc_id]

    def required_coverage(self, doc_id):
        return [list(cell) for cell in self.documents[doc_id]["required_coverage"]]

    def page_config(self, doc_id, revision, page_no):
        pages = self.revision_config(doc_id, revision)["pages"]
        for page in pages:
            if int(page["page_no"]) == int(page_no):
                return page
        raise PortalError("page_not_found", doc_id=doc_id, revision=revision,
                          page_no=page_no)

    def page_url(self, doc_id, revision, page_no):
        return f"mock://vendor/{doc_id}/{revision}/page/{int(page_no)}"

    def archive_url(self, doc_id, revision):
        return f"mock://vendor/{doc_id}/archive/{revision}"

    def _page_raw(self, doc_id, revision, page):
        body = {
            "doc_id": doc_id,
            "page_no": int(page["page_no"]),
            "revision": revision,
            "rows": page["rows"],
        }
        return canonical(body)

    def _etag(self, doc_id, revision, page):
        if page.get("etag"):
            return page["etag"]
        digest = sha256(self._page_raw(doc_id, revision, page))[:16]
        return f'"{revision.lower()}-{int(page["page_no"])}-{digest}"'

    def page_signature(self, doc_id, revision, page_no, digest):
        message = f"{self.portal_id}|{doc_id}|{revision}|{int(page_no)}|{digest}"
        return hmac.new(self.signing_key.encode(), message.encode(),
                        hashlib.sha256).hexdigest()

    def archive_signature(self, manifest_bytes):
        return hmac.new(self.signing_key.encode(), manifest_bytes,
                        hashlib.sha256).hexdigest()

    def verify_page_signature(self, doc_id, revision, page_no, digest, signature):
        return hmac.compare_digest(
            self.page_signature(doc_id, revision, page_no, digest), signature)

    def _entry(self, doc_id, revision, page):
        raw = self._page_raw(doc_id, revision, page)
        digest = sha256(raw)
        return {
            "page_no": int(page["page_no"]),
            "url": self.page_url(doc_id, revision, page["page_no"]),
            "etag": self._etag(doc_id, revision, page),
            "sha256": digest,
            "signature": self.page_signature(doc_id, revision, page["page_no"], digest),
            "size": len(raw),
        }

    # ------------------------------------------------------------- discovery
    def manifest(self, doc_id):
        cfg = self.documents.get(doc_id)
        if cfg is None:
            raise PortalError("unknown_document", doc_id=doc_id)
        revision = self.current[doc_id]
        pages = list(cfg["revisions"][revision]["pages"])
        total = len(pages)
        queued = self._discoveries.get(doc_id) or []
        if queued:
            total = int(queued.pop(0))
            pages = [p for p in pages if int(p["page_no"]) <= total]
        entries = [self._entry(doc_id, revision, p) for p in pages]
        return {
            "portal_id": self.portal_id,
            "doc_id": doc_id,
            "revision": revision,
            "total_pages": total,
            "coverage": self.required_coverage(doc_id),
            "pages": entries,
            "discovered_at": self.clock.iso(),
        }

    def discover(self, doc_id):
        return self.manifest(doc_id)

    # ----------------------------------------------------------------- leases
    def request_lease(self, doc_id, holder="night-shift", ttl_seconds=None):
        revision = self.current[doc_id]
        cfg = self.revision_config(doc_id, revision)
        lease_cfg = cfg.get("lease")
        if not lease_cfg:
            raise PortalError("lease_not_offered", doc_id=doc_id, revision=revision)
        ttl = int(ttl_seconds or self.scenario.get("lease_ttl_seconds")
                  or lease_cfg.get("ttl_seconds", 900))
        issued = self.clock.now()
        expires = issued + timedelta(seconds=ttl)
        material = f"{self.portal_id}|{doc_id}|{revision}|{holder}|{issued.isoformat()}|{ttl}"
        lease_id = "lease-" + hmac.new(self.signing_key.encode(), material.encode(),
                                       hashlib.sha256).hexdigest()[:16]
        record = {
            "lease_id": lease_id,
            "doc_id": doc_id,
            "revision": revision,
            "holder": holder,
            "issued_at": self.clock.iso(),
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S"),
            "ttl_seconds": ttl,
            "status": "active",
        }
        self._leases[lease_id] = record
        return dict(record)

    def _check_lease(self, doc_id, revision, lease_id):
        if not lease_id or lease_id not in self._leases:
            raise PortalError("lease_required", doc_id=doc_id, revision=revision)
        lease = self._leases[lease_id]
        if lease["doc_id"] != doc_id or lease["revision"] != revision:
            raise PortalError("lease_wrong_scope", doc_id=doc_id, revision=revision)
        expires = datetime.strptime(lease["expires_at"], "%Y-%m-%dT%H:%M:%S")
        if self.clock.now() >= expires:
            lease["status"] = "expired"
            raise PortalError("lease_expired", lease_id=lease_id,
                              expires_at=lease["expires_at"], now=self.clock.iso())
        return lease

    # ------------------------------------------------------------------ fetch
    def _flip_if_needed(self):
        after = self.scenario.get("flip_after_fetches")
        every = self.scenario.get("flip_every_fetch")
        target = self.scenario.get("flip_to")
        if every and self._fetch_count and self._fetch_count % int(every) == 0:
            self._toggle_current(target)
        elif after is not None and self._fetch_count >= int(after):
            self._toggle_current(target)

    def _toggle_current(self, target):
        for doc in self.current:
            revisions = list(self.documents[doc]["revisions"])
            if target and target in revisions:
                self.current[doc] = target
            else:
                idx = revisions.index(self.current[doc])
                self.current[doc] = revisions[(idx + 1) % len(revisions)]

    def fetch_page(self, url, etag=None, lease_id=None):
        self._fetch_count += 1
        self._flip_if_needed()
        parts = url.split("/")
        # mock://vendor/<doc>/<revision>/page/<n>
        if len(parts) != 7 or parts[5] != "page":
            return PortalResponse(404, error={"code": "not_found", "url": url})
        doc_id = parts[3]
        revision = parts[4]
        page_no = int(parts[6])
        try:
            page = self.page_config(doc_id, revision, page_no)
        except PortalError as error:
            return PortalResponse(404, error={"code": error.code, **error.detail})

        cfg = self.revision_config(doc_id, revision)
        if cfg.get("lease") and revision == self.current.get(doc_id):
            try:
                self._check_lease(doc_id, revision, lease_id)
            except PortalError as error:
                return PortalResponse(403, error={"code": error.code, **error.detail})

        fault_key = f"{revision}/{page_no}"
        queue = self._faults.get(fault_key)
        fault = None
        if queue:
            fault = queue.pop(0) if isinstance(queue, list) else queue
        if fault:
            kind = fault.get("kind")
            if kind == "timeout":
                raise PortalTimeout(f"{doc_id}/{revision}/{page_no}")
            if kind in ("http_503", "http_500", "http_502", "http_504"):
                return PortalResponse(int(kind.split("_")[1]),
                                      error={"code": "transient", "fault": kind})
            if kind == "gone":
                return PortalResponse(410, error={"code": "page_gone"})
            if kind == "unauthorized":
                return PortalResponse(403, error={"code": "not_authorized"})

        raw = self._page_raw(doc_id, revision, page)
        entry = self._entry(doc_id, revision, page)

        tampered = set(self.scenario.get("tamper_pages", []))
        if fault_key in tampered:
            raw = raw.replace(b'"max_kg":', b'"max_kg":1', 1)
            entry = dict(entry, size=len(raw), sha256=sha256(raw))

        if etag and etag == entry["etag"]:
            return PortalResponse(304, headers={"etag": entry["etag"]},
                                  error={"code": "not_modified"})

        if self.scenario.get("stale_304"):
            for other_revision in self.documents[doc_id]["revisions"]:
                try:
                    other = self.page_config(doc_id, other_revision, page_no)
                except PortalError:
                    continue
                other_etag = self._etag(doc_id, other_revision, other)
                if other_revision != revision and other_etag == etag:
                    return PortalResponse(304, headers={"etag": etag},
                                          error={"code": "stale_not_modified",
                                                 "served_revision": other_revision})

        count = self._delivery.get(fault_key, 0)
        self._delivery[fault_key] = count + 1
        duplicate = self.scenario.get("duplicate_pages", {}).get(fault_key)

        # Advance the clock *before* checking the lease, so a slow transfer
        # can legitimately consume the remaining lease lifetime.
        tick = self.scenario.get("tick_seconds_per_fetch")
        if tick:
            self.clock.advance(int(tick))

        if cfg.get("lease") and revision == self.current.get(doc_id):
            try:
                self._check_lease(doc_id, revision, lease_id)
            except PortalError as error:
                return PortalResponse(403, error={"code": error.code, **error.detail})

        headers = {"etag": entry["etag"], "x-page-sha256": entry["sha256"],
                   "x-page-signature": entry["signature"],
                   "x-revision": revision, "x-page-no": str(page_no)}
        if duplicate and count + 1 <= int(duplicate):
            headers["x-duplicate-delivery"] = str(count + 1)
        return PortalResponse(200, raw, headers)

    fetch = fetch_page

    # ---------------------------------------------------------------- archive
    def fetch_archive(self, doc_id, revision=None):
        revision = revision or "V1"
        try:
            cfg = self.revision_config(doc_id, revision)
        except PortalError as error:
            return PortalResponse(404, error={"code": error.code, **error.detail})
        archive = cfg.get("archive")
        if not archive or self.scenario.get("archive_denied"):
            return PortalResponse(403, error={
                "code": "archive_not_published", "doc_id": doc_id,
                "revision": revision,
                "reason": "monthly archive only exists for closed revisions"})

        pages = []
        payloads = {}
        for page in cfg["pages"]:
            raw = self._page_raw(doc_id, revision, page)
            entry = self._entry(doc_id, revision, page)
            pages.append(entry)
            payloads[entry["page_no"]] = raw
        manifest = {
            "portal_id": self.portal_id,
            "doc_id": doc_id,
            "revision": revision,
            "archive_id": archive["archive_id"],
            "issued_at": archive["issued_at"],
            "total_pages": len(pages),
            "coverage": self.required_coverage(doc_id),
            "pages": pages,
        }
        manifest_bytes = canonical(manifest)
        signature = self.archive_signature(manifest_bytes)

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
            info = zipfile.ZipInfo("manifest.json", date_time=(2026, 11, 1, 0, 0, 0))
            zf.writestr(info, manifest_bytes)
            info = zipfile.ZipInfo("signature.txt", date_time=(2026, 11, 1, 0, 0, 0))
            zf.writestr(info, signature.encode())
            for page_no in sorted(payloads):
                info = zipfile.ZipInfo(f"pages/page-{page_no}.json",
                                       date_time=(2026, 11, 1, 0, 0, 0))
                zf.writestr(info, payloads[page_no])
        return PortalResponse(200, buffer.getvalue(),
                              {"content-type": "application/zip",
                               "x-archive-id": archive["archive_id"],
                               "x-revision": revision})
