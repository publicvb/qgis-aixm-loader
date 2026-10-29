"""Turn unresolved ``Geom`` descriptions into coordinate lists.

Needs lookups supplied by the dataset:
  point_lookup(Ref) -> (lon, lat) or None          xlinked points (DesignatedPoint, Navaid, ...)
  curve_lookup(Ref) -> [(lon, lat), ...] or None   xlinked curves (GeoBorder)
  ref_label(Ref) -> str or None                    readable name for messages

Everything that does not fit (gaps between consecutive segments, vertices
off the border they should follow, unclosed rings, unresolved xlinks,
geometry errors) is reported as a located ``GeomIssue``.
"""
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import geodesy
from .model import Ref

# junction tolerance between consecutive segments, in degrees
_EPS = 1e-9
# computed arc end points within this distance (m) of the published
# neighbouring vertex are snapped onto it (removes junction bow-ties)
SNAP_M = 500.0


@dataclass
class GeomIssue:
    kind: str  # unresolved_reference | geometry_error | junction_gap | off_border | ring_not_closed
    message: str
    location: Optional[Tuple[float, float]] = None
    size_m: Optional[float] = None
    severity: str = "warning"

    def __str__(self):
        return self.message


@dataclass
class Resolved:
    kind: str
    points: list = field(default_factory=list)  # [(lon, lat)]
    lines: list = field(default_factory=list)  # [[(lon, lat)]]
    polygons: list = field(default_factory=list)  # [[ring [(lon, lat)]]]
    issues: List[GeomIssue] = field(default_factory=list)
    # published (non-computed) boundary vertices: segment control points and
    # followed border vertices, used to check alignment with neighbours
    vertices: list = field(default_factory=list)

    @property
    def empty(self):
        return not (self.points or self.lines or self.polygons)


class GeometryResolver:
    def __init__(self, point_lookup, curve_lookup, ref_label=None, densify_m=5000.0,
                 arc_step_deg=1.0, tolerance_m=50.0):
        self.point_lookup = point_lookup
        self.curve_lookup = curve_lookup
        self.ref_label = ref_label or (lambda ref: None)
        self.densify_m = densify_m
        self.arc_step = arc_step_deg
        self.tolerance_m = tolerance_m

    def resolve(self, geom):
        out = Resolved(geom.kind)
        if geom.error:
            out.issues.append(GeomIssue("geometry_error", geom.error, severity="error"))
            return out
        if geom.kind == "point":
            for p in geom.points:
                xy = self._point(p, out.issues)
                if xy is not None:
                    out.points.append(xy)
        elif geom.kind == "line":
            for segs in geom.lines:
                coords = self._chain(segs, out.issues, closed=False, ctrl=out.vertices)
                if len(coords) >= 2:
                    out.lines.append(coords)
        else:
            for rings in geom.polygons:
                poly = []
                for i, segs in enumerate(rings):
                    ring = self._chain(segs, out.issues, closed=True, ctrl=out.vertices)
                    if len(ring) >= 4:
                        poly.append(ring)
                    elif i == 0:
                        out.issues.append(GeomIssue("geometry_error", "degenerate exterior ring",
                                                    ring[0] if ring else None, severity="error"))
                        break
                if poly:
                    out.polygons.append(poly)
        return out

    # ------------------------------------------------------------------
    def _name(self, ref):
        label = self.ref_label(ref) or ref.title
        return "%s (%s)" % (label, ref.href) if label else ref.href

    def _point(self, p, issues):
        if isinstance(p, Ref):
            xy = self.point_lookup(p)
            if xy is None:
                issues.append(GeomIssue("unresolved_reference", "unresolved point %s" % p.href,
                                        severity="error"))
            return xy
        return p

    def _points(self, pts, issues):
        return [xy for xy in (self._point(p, issues) for p in pts) if xy is not None]

    def _seg_coords(self, seg, issues, ctrl=None):
        if seg.kind in ("linear", "geodesic", "arc3", "arcstring") and ctrl is not None:
            ctrl.extend(self._points(seg.points, []))
        if seg.kind == "linear":
            return self._points(seg.points, issues)
        if seg.kind == "geodesic":
            return geodesy.densify_great_circle(self._points(seg.points, issues), self.densify_m)
        if seg.kind in ("arc", "circle"):
            c = self._points(seg.points[:1], issues)
            if not c:
                return []
            if seg.kind == "circle":
                return geodesy.circle_points(c[0], seg.radius, self.arc_step)
            return geodesy.arc_points(c[0], seg.radius, seg.start, seg.end, self.arc_step)
        if seg.kind in ("arc3", "circle3", "arcstring"):
            pts = self._points(seg.points, issues)
            if len(pts) < 3:
                return pts
            if seg.kind == "circle3":
                return geodesy.arc_through(pts[0], pts[1], pts[2], self.arc_step, True)
            out = []
            for i in range(0, len(pts) - 2, 2):
                arc = geodesy.arc_through(pts[i], pts[i + 1], pts[i + 2], self.arc_step)
                out.extend(arc if not out else arc[1:])
            return out
        return []

    def _chain(self, segs, issues, closed, ctrl=None):
        """Concatenate segments; xlinked curves are followed between neighbours."""
        parts = [None if s.kind == "ref" else self._seg_coords(s, issues, ctrl) for s in segs]
        n = len(segs)
        for i, s in enumerate(segs):
            if s.kind != "ref":
                continue
            curve = self.curve_lookup(s.ref)
            if not curve:
                issues.append(GeomIssue("unresolved_reference", "unresolved curve %s" % s.ref.href,
                                        _neighbour_end(parts, i, -1, closed), severity="error"))
                parts[i] = []
                continue
            prev_pt = _neighbour_end(parts, i, -1, closed)
            next_pt = _neighbour_end(parts, i, +1, closed)
            if n == 1 or (prev_pt is None and next_pt is None):
                parts[i] = list(curve)
                continue
            parts[i] = follow_curve(curve, prev_pt or curve[0], next_pt or curve[-1])
            if ctrl is not None:
                ctrl.extend(parts[i][1:-1])  # border vertices; the projected ends are computed
            for anchor, on_border in ((prev_pt, parts[i][0]), (next_pt, parts[i][-1])):
                if anchor is None:
                    continue
                d = geodesy.distance(anchor, on_border)
                if d > self.tolerance_m:
                    issues.append(GeomIssue(
                        "off_border", "vertex %.0f m off border %s" % (d, self._name(s.ref)),
                        anchor, d))
        self._check_junctions(segs, parts, issues, closed)
        self._snap_computed_ends(segs, parts, closed)
        coords = []
        for part in parts:
            for xy in part:
                if not coords or not _same(coords[-1], xy):
                    coords.append(xy)
        if closed and coords and not _same(coords[0], coords[-1]):
            coords.append(coords[0])
        return coords

    def _check_junctions(self, segs, parts, issues, closed):
        """Report consecutive segments that do not meet within the tolerance."""
        idx = [i for i, p in enumerate(parts) if p]
        pairs = [(a, b, False) for a, b in zip(idx, idx[1:])]
        if closed and idx:
            pairs.append((idx[-1], idx[0], True))
        n = len(parts)
        for a, b, closing in pairs:
            between = [(a + k) % n for k in range(1, (b - a) % n or n)]
            if any(segs[j].kind == "ref" and not parts[j] for j in between):
                continue  # spans an unresolved curve, already reported as such
            end, start = parts[a][-1], parts[b][0]
            d = geodesy.distance(end, start)
            if d <= self.tolerance_m:
                continue
            if closing:
                kind = "ring_not_closed"
                what = "ring does not close: %.0f m between end of segment %d and start of segment %d" % (
                    d, a + 1, b + 1)
            else:
                kind = "junction_gap"
                what = "gap of %.0f m between segment %d (%s) and segment %d (%s)%s" % (
                    d, a + 1, segs[a].kind, b + 1, segs[b].kind,
                    "; arc end snapped" if d <= SNAP_M and "arc" in (segs[a].kind, segs[b].kind)
                    else "; joined by a straight line")
            issues.append(GeomIssue(kind, what, ((end[0] + start[0]) / 2, (end[1] + start[1]) / 2), d))

    def _snap_computed_ends(self, segs, parts, closed):
        """Snap computed arc end points onto the published neighbouring vertices.

        Published coordinates are rounded, so an arc computed from centre,
        radius and angles typically ends 10-30 m from the next published
        point, which creates tiny self-intersections at the junction.
        """
        n = len(parts)
        for i, s in enumerate(segs):
            if s.kind not in ("arc", "arc3", "arcstring") or len(parts[i]) < 2:
                continue
            prev_pt = _neighbour_end(parts, i, -1, closed) if (closed or i > 0) else None
            next_pt = _neighbour_end(parts, i, +1, closed) if (closed or i < n - 1) else None
            part = parts[i]
            if prev_pt is not None and geodesy.distance(prev_pt, part[0]) < SNAP_M:
                part[0] = prev_pt
            if next_pt is not None and geodesy.distance(next_pt, part[-1]) < SNAP_M:
                part[-1] = next_pt


def _neighbour_end(parts, i, step, closed):
    n = len(parts)
    j = i + step
    while 0 <= j < n or closed:
        j %= n
        if j == i:
            return None
        p = parts[j]
        if p:
            return p[-1] if step < 0 else p[0]
        if p is None:  # another unresolved ref: no anchor
            return None
        j += step
    return None


def _same(a, b):
    return abs(a[0] - b[0]) < _EPS and abs(a[1] - b[1]) < _EPS


# -- following a border between two points ------------------------------
def _scale(curve):
    """Longitude scale so that planar distances in degrees are roughly isotropic."""
    lats = [c[1] for c in curve]
    return math.cos(math.radians((min(lats) + max(lats)) / 2))


def _project(curve, p, k):
    """(distance along, projected point, total length) of p on the curve."""
    best = (float("inf"), 0.0, curve[0])
    along = 0.0
    for a, b in zip(curve, curve[1:]):
        ax, ay, bx, by = a[0] * k, a[1], b[0] * k, b[1]
        px, py = p[0] * k, p[1]
        dx, dy = bx - ax, by - ay
        seg_len = math.hypot(dx, dy)
        t = 0.0 if seg_len == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (seg_len ** 2)))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(px - qx, py - qy)
        if d < best[0]:
            best = (d, along + t * seg_len, (qx / k, qy))
        along += seg_len
    return best[1], best[2], along


def _substring(curve, d0, d1, k):
    """Vertices of the curve strictly between distances d0 < d1."""
    out, along = [], 0.0
    for a, b in zip(curve, curve[1:]):
        along += math.hypot((b[0] - a[0]) * k, b[1] - a[1])
        if d0 < along < d1:
            out.append(b)
    return out


def follow_curve(curve, start, end):
    """Part of ``curve`` from the point nearest ``start`` to the one nearest ``end``.

    For a closed curve the shorter way round is taken.
    """
    k = _scale(curve)
    d0, p0, total = _project(curve, start, k)
    d1, p1, _ = _project(curve, end, k)
    if d0 <= d1:
        forward = [p0] + _substring(curve, d0, d1, k) + [p1]
        fwd_len = d1 - d0
    else:
        forward = [p0] + list(reversed(_substring(curve, d1, d0, k))) + [p1]
        fwd_len = d0 - d1
    if _same(curve[0], curve[-1]) and total - fwd_len < fwd_len:
        # the shorter way passes the closing vertex
        if d0 <= d1:
            way = ([p0] + list(reversed(_substring(curve, 0, d0, k))) + [curve[0]]
                   + list(reversed(_substring(curve, d1, total, k))))
        else:
            way = [p0] + _substring(curve, d0, total, k) + [curve[0]] + _substring(curve, 0, d1, k)
        return way + [p1]
    return forward
