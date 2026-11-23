"""Offline capture of city GIS exclusion bulletins and spatial ruling evidence.

A city bulletin arrives as GeoJSON polygons carrying a declared CRS, a
boundary convention, effective/revocation windows and a control-point error
budget.  ``capture_event`` validates the bulletin, transforms every ring into
site plane coordinates and stores the normalized zones next to the raw
GeoJSON so every later ruling can be recomputed from the same source.

Spatial ruling protocol (per route segment or landing point, per zone):

- ``禁止``   the geometry certainly enters the forbidden region (its
             interior, plus the boundary itself when the bulletin declares a
             closed boundary);
- ``待测``   the geometry stays outside the forbidden region but inside the
             uncertainty band of width ``error_m`` around it, so the current
             evidence cannot prove it clear and it must not be released;
- ``可通行`` the geometry is provably outside forbidden region + error band.

A route may only be released when every segment is ``可通行``; an emergency
landing site is usable only when its point verdict is ``可通行``.  Anything
that cannot be interpreted (bad rings, unknown CRS, missing bulletin
revision, ambiguous re-import) fails closed and never degrades into an
empty exclusion zone that would silently let a route pass.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

EPS = 1e-9
SITE_CRS = "site-local"

VERDICT_FORBIDDEN = "禁止"
VERDICT_UNCERTAIN = "待测"
VERDICT_CLEAR = "可通行"


# ---------------------------------------------------------------------------
# CRS transform
# ---------------------------------------------------------------------------

def _crs_transform(crs):
    """Compile a declared CRS into a point transform to site plane coordinates."""
    if not isinstance(crs, dict) or "name" not in crs:
        raise ValueError("gis bulletin must declare a crs object with a name")
    name = crs["name"]
    if name == SITE_CRS:
        return lambda p: [float(p[0]), float(p[1])]
    if name == "city-grid":
        origin = crs.get("origin", [0.0, 0.0])
        if (not isinstance(origin, (list, tuple)) or len(origin) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in origin)):
            raise ValueError("city-grid crs requires a numeric origin [x, y]")
        scale = crs.get("scale", 1.0)
        if (not isinstance(scale, (int, float)) or isinstance(scale, bool)
                or not math.isfinite(scale) or scale == 0):
            raise ValueError("city-grid crs requires a nonzero finite scale")
        swap = bool(crs.get("swap_axes", False))

        def transform(p):
            x, y = float(p[0]), float(p[1])
            if swap:
                x, y = y, x
            return [(x - origin[0]) * scale, (y - origin[1]) * scale]

        return transform
    raise ValueError(f"unsupported crs: {name}")


# ---------------------------------------------------------------------------
# GeoJSON validation
# ---------------------------------------------------------------------------

def _validate_ring(ring, where):
    if not isinstance(ring, (list, tuple)) or len(ring) < 4:
        raise ValueError(f"ring {where} must contain at least 4 positions")
    out = []
    for p in ring:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           and math.isfinite(v) for v in p)):
            raise ValueError(f"ring {where} contains a non-numeric position")
        out.append([float(p[0]), float(p[1])])
    if out[0] != out[-1]:
        raise ValueError(f"ring {where} is not closed")
    return out


def _polygons(geojson):
    """Extract (coordinates, properties) pairs for every polygon in a GeoJSON object."""
    if not isinstance(geojson, dict):
        raise ValueError("geojson payload must be an object")
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        features = geojson.get("features")
        if not isinstance(features, list) or not features:
            raise ValueError("geojson feature collection is empty")
        pairs = [((f or {}).get("geometry"), (f or {}).get("properties") or {})
                 for f in features]
    elif kind == "Feature":
        pairs = [(geojson.get("geometry"), geojson.get("properties") or {})]
    else:
        pairs = [(geojson, {})]
    polygons = []
    for geom, props in pairs:
        if not isinstance(geom, dict):
            raise ValueError("geojson feature is missing its geometry")
        gtype = geom.get("type")
        if gtype == "Polygon":
            polygons.append((geom.get("coordinates"), props))
        elif gtype == "MultiPolygon":
            for coords in geom.get("coordinates") or []:
                polygons.append((coords, props))
        else:
            raise ValueError(f"unsupported geometry type: {gtype}")
    if not polygons:
        raise ValueError("geojson contains no polygon geometry")
    return polygons


# ---------------------------------------------------------------------------
# Bulletin capture
# ---------------------------------------------------------------------------

def capture_event(event):
    """Validate and normalize a kind="gis" bulletin event.

    Returns a new event dict with normalized ``zones`` in site plane
    coordinates plus the effective error budget.  Raises ValueError for any
    bulletin that cannot be interpreted, so a bad GIS file fails the import
    instead of becoming an empty exclusion zone.
    """
    if not isinstance(event, dict) or event.get("kind") != "gis":
        raise ValueError("gis capture requires an event of kind 'gis'")
    for key in ("id", "bulletin_id", "revision"):
        if not event.get(key):
            raise ValueError(f"gis bulletin is missing {key}")
    revision = event["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("gis bulletin revision must be a positive integer")
    boundary = event.get("boundary", "closed")
    if boundary not in ("closed", "open"):
        raise ValueError("boundary convention must be 'closed' or 'open'")
    retraction = event.get("retraction", "forward")
    if retraction not in ("forward", "retroactive"):
        raise ValueError("retraction must be 'forward' or 'retroactive'")
    start, end = event.get("effective_from"), event.get("effective_until")
    revoked_at = event.get("revoked_at")
    if not start or not end or not isinstance(start, str) or not isinstance(end, str) or end < start:
        raise ValueError("gis bulletin has an invalid effective window")
    if revoked_at is not None and not isinstance(revoked_at, str):
        raise ValueError("revoked_at must be a timestamp string")
    error = event.get("control_error_m")
    if (not isinstance(error, (int, float)) or isinstance(error, bool)
            or not math.isfinite(error) or error < 0):
        raise ValueError("control_error_m must be a non-negative number")
    error, error_source = float(error), "public_bound"
    survey = event.get("survey")
    if survey is not None:
        if (not isinstance(survey, dict) or not survey.get("signed_by")
                or not survey.get("control_points")
                or not isinstance(survey.get("error_m"), (int, float))
                or isinstance(survey.get("error_m"), bool)
                or not math.isfinite(survey["error_m"]) or survey["error_m"] < 0):
            raise ValueError("signed survey requires signed_by, control_points "
                             "and a non-negative error_m")
        error, error_source = float(survey["error_m"]), "signed_survey"
    out = dict(event)
    out["boundary"] = boundary
    out["retraction"] = retraction
    out["error_m"] = error
    out["error_source"] = error_source
    geojson = event.get("geojson")
    if geojson is None:
        if not (retraction == "retroactive" and revoked_at):
            raise ValueError("a gis bulletin without geometry is only valid "
                             "as a retroactive retraction")
        out["zones"] = []
        return out
    transform = _crs_transform(event.get("crs"))
    zone_end = min(end, revoked_at) if (revoked_at and retraction == "forward") else end
    zones = []
    for i, (coords, props) in enumerate(_polygons(geojson)):
        if not isinstance(coords, (list, tuple)) or not coords:
            raise ValueError(f"polygon {i} has no rings")
        rings = []
        for j, ring in enumerate(coords):
            validated = _validate_ring(ring, f"{i}/{j}")
            rings.append([transform(p) for p in validated])
        zones.append({
            "zone_id": f"{event['bulletin_id']}:r{revision}:{i}",
            "name": (props or {}).get("name"),
            "rings": rings,
            "boundary": boundary,
            "start": start,
            "end": zone_end,
            "error_m": error,
            "error_source": error_source,
            "source": {"bulletin_id": event["bulletin_id"], "revision": revision,
                       "crs": event.get("crs")},
        })
    out["zones"] = zones
    return out


def resolve_events(events):
    """Resolve plan zone references against the captured gis bulletins.

    Plan events may carry ``{"bulletin": id, "revision": n}`` zone entries;
    each is replaced by the normalized zones of that exact bulletin revision
    so plan ledger and site ledger always rule against the same map version.
    A retroactive retraction voids every revision of its bulletin (the zone
    is kept as evidence but never enforced).  Missing or ambiguous
    references stay attached and fail closed downstream.
    """
    bulletins = defaultdict(dict)
    ambiguous = set()
    for event in events:
        if event.get("kind") != "gis":
            continue
        bid, rev = event.get("bulletin_id"), event.get("revision")
        slot = bulletins[bid]
        fingerprint = json.dumps(event.get("zones", []), sort_keys=True)
        if rev in slot and json.dumps(slot[rev].get("zones", []), sort_keys=True) != fingerprint:
            ambiguous.add(bid)
        slot[rev] = event
    voided = {bid for bid, revisions in bulletins.items()
              if any(e.get("retraction") == "retroactive" and e.get("revoked_at")
                     for e in revisions.values())}
    for event in events:
        if event.get("kind") != "plan":
            continue
        resolved, missing = [], False
        for zone in event.get("zones", []):
            if not isinstance(zone, dict) or "bulletin" not in zone:
                resolved.append(zone)
                continue
            bid = zone.get("bulletin")
            try:
                rev = int(zone.get("revision"))
            except (TypeError, ValueError):
                rev = None
            source = bulletins.get(bid, {}).get(rev) if rev else None
            if source is None or bid in ambiguous:
                missing = True
                resolved.append({"bulletin": bid, "revision": zone.get("revision"),
                                 "unresolved": True})
                continue
            for captured in source.get("zones", []):
                entry = dict(captured)
                entry["ref"] = {"bulletin": bid, "revision": rev}
                if bid in voided:
                    entry["void"] = True
                    entry["void_reason"] = "公告被追溯撤销，禁区自始不生效"
                resolved.append(entry)
        event["zones"] = resolved
        if missing:
            event["zones_unresolved"] = True
    return events


# ---------------------------------------------------------------------------
# Plane geometry primitives
# ---------------------------------------------------------------------------

def _dist_point_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _point_on_segment(p, a, b):
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    if abs(cross) > EPS:
        return False
    return (min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS
            and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS)


def _orient(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def _segments_cross(a, b, c, d):
    """Proper crossing: the segments meet at a point interior to both."""
    return _orient(a, b, c) * _orient(a, b, d) < 0 and _orient(c, d, a) * _orient(c, d, b) < 0


def _segments_touch(a, b, c, d):
    """Any contact at all, including endpoint touches and collinear overlap."""
    if _segments_cross(a, b, c, d):
        return True
    return any(_point_on_segment(p, s, e)
               for p, s, e in ((a, c, d), (b, c, d), (c, a, b), (d, a, b)))


def _dist_segment_segment(a, b, c, d):
    if _segments_touch(a, b, c, d):
        return 0.0
    return min(_dist_point_segment(a, c, d), _dist_point_segment(b, c, d),
               _dist_point_segment(c, a, b), _dist_point_segment(d, a, b))


def _point_in_ring(p, ring):
    """Even-odd ray casting; boundary points are reported separately by the caller."""
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def _ring_edges(rings):
    for ring in rings:
        yield from zip(ring, ring[1:])


def _check_rings(rings):
    if not isinstance(rings, list) or not rings:
        raise ValueError("zone has no rings")
    for ring in rings:
        if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
            raise ValueError("zone ring is not closed")


def _point_zone_geometry(p, rings):
    """(strictly inside filled region, on its boundary, distance to it)."""
    min_edge = math.inf
    on_boundary = False
    for a, b in _ring_edges(rings):
        d = _dist_point_segment(p, a, b)
        if d < min_edge:
            min_edge = d
        if d <= EPS:
            on_boundary = True
    inside = (_point_in_ring(p, rings[0])
              and not any(_point_in_ring(p, hole) for hole in rings[1:]))
    inside_strict = inside and not on_boundary
    distance = 0.0 if (inside_strict or on_boundary) else min_edge
    return inside_strict, on_boundary, distance


def _verdict(inside_or_crosses, touches, distance, boundary, error, hit_reason):
    if inside_or_crosses:
        return VERDICT_FORBIDDEN, hit_reason, 0.0
    if touches and boundary == "closed":
        return VERDICT_FORBIDDEN, "接触闭合边界，边界属于禁区", 0.0
    if error > 0 and distance <= error:
        return VERDICT_UNCERTAIN, "进入测量误差不确定带", distance
    return VERDICT_CLEAR, "与禁区边界的最短距离大于总误差", distance


def _evidence(zone, verdict, reason, distance):
    source = zone.get("source") or {}
    return {"verdict": verdict, "reason": reason,
            "zone_id": zone.get("zone_id"), "zone_name": zone.get("name"),
            "bulletin": source.get("bulletin_id"), "revision": source.get("revision"),
            "boundary": zone.get("boundary", "closed"),
            "distance_m": distance, "error_m": float(zone.get("error_m", 0.0))}


def point_zone_verdict(p, zone):
    """Verdict for a single point against one normalized polygon zone."""
    rings = zone["rings"]
    _check_rings(rings)
    inside, on_boundary, distance = _point_zone_geometry(p, rings)
    verdict, reason, distance = _verdict(
        inside, on_boundary, distance, zone.get("boundary", "closed"),
        float(zone.get("error_m", 0.0)), "位于禁区内部")
    return verdict, _evidence(zone, verdict, reason, distance)


def segment_zone_verdict(a, b, zone):
    """Verdict for one route segment against one normalized polygon zone."""
    rings = zone["rings"]
    _check_rings(rings)
    probes = [a, b, [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0]]
    hits_interior = any(_point_zone_geometry(p, rings)[0] for p in probes)
    touches = False
    distance = math.inf
    for c, d in _ring_edges(rings):
        if _segments_cross(a, b, c, d):
            hits_interior = True
        if _segments_touch(a, b, c, d):
            touches = True
        edge = _dist_segment_segment(a, b, c, d)
        if edge < distance:
            distance = edge
    if hits_interior:
        touches, distance = True, 0.0
    verdict, reason, distance = _verdict(
        hits_interior, touches, distance, zone.get("boundary", "closed"),
        float(zone.get("error_m", 0.0)), "线段穿越禁区内部")
    return verdict, _evidence(zone, verdict, reason, distance)


def route_zone_evidence(route, zone):
    """Per-segment verdicts of a whole route against one normalized zone."""
    evidence = []
    for i, (a, b) in enumerate(zip(route, route[1:])):
        verdict, entry = segment_zone_verdict(a, b, zone)
        entry["segment_index"] = i
        entry["segment"] = [list(a), list(b)]
        evidence.append(entry)
    return evidence
