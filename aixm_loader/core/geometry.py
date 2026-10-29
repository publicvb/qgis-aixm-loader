"""Parse GML 3.2 / AIXM geometry elements into unresolved segment descriptions.

Coordinates are normalised to (lon, lat) at parse time. Anything that needs
other features (xlinked border curves, xlinked points) or geodesic
computation is kept as a ``Seg`` description and turned into coordinates
later by ``core.resolve.GeometryResolver``.

Problems never abort the feature: an unparseable geometry becomes a ``Geom``
with ``error`` set, and notes (approximations, axis swaps) are collected in
``GmlParser.notes`` for the feature being parsed.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import Ref
from .xmlutil import (
    GML_ID, XLINK_HREF, XLINK_TITLE, child, children, elements, is_gml, local, parse_href, text,
)
from . import geodesy

# AIXM elements that wrap GML geometry and add AIXM properties (elevation, ...)
AIXM_GEOMETRY = {
    "Point": "point", "ElevatedPoint": "point",
    "Curve": "line", "ElevatedCurve": "line",
    "Surface": "polygon", "ElevatedSurface": "polygon",
}
GML_GEOMETRY = {
    "Point": "point", "MultiPoint": "point",
    "Curve": "line", "LineString": "line", "OrientableCurve": "line",
    "CompositeCurve": "line", "MultiCurve": "line",
    "Surface": "polygon", "Polygon": "polygon", "PolygonPatch": "polygon",
    "MultiSurface": "polygon", "CompositeSurface": "polygon",
}
# GML segment types without an exact construction here: control points are used
_APPROXIMATED = {"ArcByBulge", "ArcStringByBulge", "CubicSpline", "BSpline", "Bezier",
                 "Clothoid", "OffsetCurve"}

# EPSG geographic CRSs whose axis order is latitude, longitude
_GEOGRAPHIC_LATLON = {4326, 4258, 4269, 4230, 4283, 4612, 4617, 4167, 4979, 4937, 4171, 4148}
_EPSG_RE = re.compile(r"EPSG[:/]+(?:[\d.]*[:/]+)?(\d+)\s*$")

_ANGLE_UOM = {
    "deg": 1.0, "degree": 1.0, "degrees": 1.0, "urn:ogc:def:uom:epsg::9102": 1.0,
    "rad": 180.0 / math.pi, "radian": 180.0 / math.pi, "urn:ogc:def:uom:epsg::9101": 180.0 / math.pi,
    "grad": 0.9, "gon": 0.9, "urn:ogc:def:uom:epsg::9105": 0.9,
}


def is_geometry(el):
    name = local(el.tag)
    if is_gml(el):
        return name in GML_GEOMETRY
    return name in AIXM_GEOMETRY


class GeometryParseError(ValueError):
    pass


@dataclass
class Seg:
    """One curve segment, resolved to coordinates later.

    kind:
      linear / geodesic  points = list of (lon, lat) or Ref
      arc                points = [centre]; radius (m); start / end bearings
      circle             points = [centre]; radius (m)
      arc3 / circle3     points = three points on the circle
      arcstring          points = 2n+1 points, arcs through consecutive triples
      ref                ref = xlinked curve; a GeoBorder (uuid) is followed
                         between the neighbouring segments
    """

    kind: str
    points: List[Any] = field(default_factory=list)
    radius: Optional[float] = None
    start: Optional[float] = None
    end: Optional[float] = None
    ref: Optional[Ref] = None


@dataclass
class Geom:
    kind: str  # 'point' | 'line' | 'polygon'
    gml_id: Optional[str] = None
    points: List[Any] = field(default_factory=list)  # (lon, lat) or Ref
    lines: List[List[Seg]] = field(default_factory=list)
    polygons: List[List[List[Seg]]] = field(default_factory=list)  # polygon -> rings -> segs
    props: Dict[str, Any] = field(default_factory=dict)  # AIXM props on Elevated*/aixm:Point
    error: Optional[str] = None


def axis_order(srs_name, axis_labels=None):
    """'latlon', 'lonlat' or None (unsupported CRS) for a GML srsName."""
    if axis_labels:
        first = axis_labels.split()[0].lower()
        if first.startswith(("lat", "n", "φ")):
            return "latlon"
        if first.startswith(("lon", "e", "λ", "x")):
            return "lonlat"
    if not srs_name:
        return "latlon"  # AIXM default: urn:ogc:def:crs:EPSG::4326
    s = srs_name.strip().upper()
    if "CRS84" in s or "CRS:84" in s or "CRS::84" in s:
        return "lonlat"
    if "EPSG.XML#" in s:  # legacy GML form, traditionally longitude first
        return "lonlat"
    m = _EPSG_RE.search(s)
    if m and int(m.group(1)) in _GEOGRAPHIC_LATLON:
        return "latlon"
    return None


class GmlParser:
    """Parses geometry; keeps per-document registries of gml:id targets."""

    def __init__(self):
        self.geoms = {}  # gml:id -> Geom
        self.curves = {}  # gml:id -> [Seg]
        self.notes = []  # notes for the feature currently being parsed

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)

    # -- entry point ---------------------------------------------------
    def parse(self, el, srs=None, object_props=None, axis_labels=None):
        """Parse a geometry element. ``object_props`` collects AIXM props."""
        srs = el.get("srsName") or srs
        axis_labels = el.get("axisLabels") or axis_labels
        name = local(el.tag)
        kind = GML_GEOMETRY[name] if is_gml(el) else AIXM_GEOMETRY[name]
        geom = Geom(kind=kind, gml_id=el.get(GML_ID), props=object_props or {})
        try:
            order = axis_order(srs, axis_labels)
            if order is None:
                raise GeometryParseError("unsupported CRS %s (only geographic lat/lon CRS are handled)" % srs)
            ctx = (srs, axis_labels, order == "latlon")
            if kind == "point":
                geom.points = self._parse_points(el, ctx)
            elif kind == "line":
                geom.lines = self._parse_curves(el, ctx)
            else:
                geom.polygons = self._parse_surfaces(el, ctx)
        except (GeometryParseError, ValueError, TypeError) as e:
            geom.error = "%s: %s" % (name, e)
            self.note("geometry error in %s" % geom.error)
        if geom.gml_id:
            self.geoms[geom.gml_id] = geom
        return geom

    def _ctx(self, el, ctx):
        """Context updated with srsName / axisLabels declared on el."""
        srs, labels, latlon = ctx
        if el.get("srsName") or el.get("axisLabels"):
            srs = el.get("srsName") or srs
            labels = el.get("axisLabels") or labels
            order = axis_order(srs, labels)
            if order is None:
                raise GeometryParseError("unsupported CRS %s" % srs)
            latlon = order == "latlon"
        return srs, labels, latlon

    # -- points --------------------------------------------------------
    def _coords(self, el, latlon, dim=None):
        """Coordinates of a gml:pos / gml:posList / gml:coordinates element."""
        raw = (el.text or "").strip()
        if not raw:
            return []
        name = local(el.tag)
        if name == "coordinates":
            cs = el.get("cs", ",")
            ts = el.get("ts", " ")
            dec = el.get("decimal", ".")
            tuples = [t.split(cs) for t in raw.split(ts) if t]
            nums = [[float(v.replace(dec, ".")) for v in t] for t in tuples]
        else:
            vals = [float(v) for v in raw.split()]
            dim = int(el.get("srsDimension") or dim or 2)
            if name == "pos":
                dim = len(vals) if len(vals) in (2, 3) else dim
            if len(vals) % dim:
                raise GeometryParseError("posList length %d not divisible by %d" % (len(vals), dim))
            nums = [vals[i:i + dim] for i in range(0, len(vals), dim)]
        if any(len(n) < 2 for n in nums):
            raise GeometryParseError("coordinate with fewer than two values")
        pts = [(n[1], n[0]) for n in nums] if latlon else [(n[0], n[1]) for n in nums]
        if any(abs(p[1]) > 90 for p in pts) and all(abs(p[0]) <= 90 for p in pts):
            self.note("coordinates outside latitude range; axis order swapped")
            pts = [(p[1], p[0]) for p in pts]
        return pts

    def _point_items(self, el, ctx):
        """Ordered control points of a segment / ring / point element."""
        ctx = self._ctx(el, ctx)
        pts = []
        for c in elements(el):
            name = local(c.tag)
            if name in ("pos", "posList", "coordinates"):
                pts.extend(self._coords(c, self._ctx(c, ctx)[2], el.get("srsDimension")))
            elif name in ("pointProperty", "pointRep"):
                href = c.get(XLINK_HREF)
                if href:
                    kind, key = parse_href(href)
                    pts.append(Ref(kind, key, c.get(XLINK_TITLE)))
                else:
                    for p in elements(c):
                        if is_geometry(p):
                            pts.extend(self._sub(p, ctx).points)
            elif name in ("Point", "ElevatedPoint"):
                pts.extend(self._sub(c, ctx).points)
        return pts

    def _sub(self, el, ctx):
        """Parse a nested geometry, raising its error into the parent."""
        g = self.parse(el, ctx[0], axis_labels=ctx[1])
        if g.error:
            raise GeometryParseError(g.error)
        return g

    def _parse_points(self, el, ctx):
        if local(el.tag) == "MultiPoint":
            pts = []
            for m in children(el, "pointMember") + children(el, "pointMembers"):
                href = m.get(XLINK_HREF)
                if href:
                    pts.append(Ref(*parse_href(href)))
                for p in elements(m):
                    pts.extend(self._sub(p, ctx).points)
            return pts
        return self._point_items(el, ctx)

    # -- curves --------------------------------------------------------
    def _parse_curves(self, el, ctx):
        """List of lines, each a list of Seg."""
        if local(el.tag) == "MultiCurve":
            lines = []
            for m in children(el, "curveMember") + children(el, "curveMembers"):
                lines.extend(self._curve_member_lines(m, ctx))
            return lines
        return [self._curve_segs(el, ctx)]

    def _curve_member_lines(self, member, ctx):
        href = member.get(XLINK_HREF)
        if href:
            kind, key = parse_href(href)
            return [[Seg("ref", ref=Ref(kind, key, member.get(XLINK_TITLE)))]]
        lines = []
        for c in elements(member):
            if is_geometry(c):
                lines.extend(self._sub(c, ctx).lines)
        return lines

    def _curve_segs(self, el, ctx):
        name = local(el.tag)
        ctx = self._ctx(el, ctx)
        if name == "LineString":
            segs = [Seg("linear", self._point_items(el, ctx))]
        elif name == "OrientableCurve":
            base = child(el, "baseCurve")
            segs = []
            if base is not None:
                for line in self._curve_member_lines(base, ctx):
                    segs.extend(line)
            if el.get("orientation") == "-":
                segs = [_reverse_seg(s) for s in reversed(segs)]
        elif name == "CompositeCurve":
            segs = []
            for m in children(el, "curveMember"):
                for line in self._curve_member_lines(m, ctx):
                    segs.extend(line)
        else:  # gml:Curve, aixm:Curve, aixm:ElevatedCurve
            container = child(el, "segments")
            segs = []
            if container is not None:
                for s in elements(container):
                    segs.append(self._segment(s, ctx))
        gml_id = el.get(GML_ID)
        if gml_id:
            self.curves[gml_id] = segs
        return segs

    def _segment(self, s, ctx):
        name = local(s.tag)
        pts = self._point_items(s, ctx)
        latlon = self._ctx(s, ctx)[2]
        if name in ("GeodesicString", "Geodesic"):
            return Seg("geodesic", pts)
        if name == "LineStringSegment":
            interp = (s.get("interpolation") or "linear").lower()
            return Seg("geodesic" if interp == "geodesic" else "linear", pts)
        if name in ("ArcByCenterPoint", "CircleByCenterPoint"):
            radius_el = child(s, "radius")
            if radius_el is None or not pts:
                raise GeometryParseError("%s without centre or radius" % name)
            radius = geodesy.to_metres(text(radius_el), radius_el.get("uom"))
            if name == "CircleByCenterPoint":
                return Seg("circle", pts[:1], radius=radius)
            start, end = _angle(child(s, "startAngle")), _angle(child(s, "endAngle"))
            if not latlon:  # angles measured from east, counter-clockwise
                start, end = 90.0 - start, 90.0 - end
            return Seg("arc", pts[:1], radius=radius, start=start, end=end)
        if name == "Arc":
            if len(pts) != 3:
                raise GeometryParseError("Arc needs 3 points, got %d" % len(pts))
            return Seg("arc3", pts)
        if name == "ArcString":
            if len(pts) < 3 or len(pts) % 2 == 0:
                raise GeometryParseError("ArcString needs an odd number (>=3) of points, got %d" % len(pts))
            return Seg("arc3", pts) if len(pts) == 3 else Seg("arcstring", pts)
        if name == "Circle":
            if len(pts) != 3:
                raise GeometryParseError("Circle needs 3 points, got %d" % len(pts))
            return Seg("circle3", pts)
        if name in _APPROXIMATED:
            self.note("%s approximated by its control points" % name)
        else:
            self.note("unknown curve segment %s approximated by its control points" % name)
        return Seg("linear", pts)

    # -- surfaces ------------------------------------------------------
    def _parse_surfaces(self, el, ctx):
        """List of polygons, each a list of rings (exterior first)."""
        name = local(el.tag)
        ctx = self._ctx(el, ctx)
        if name in ("MultiSurface", "CompositeSurface"):
            polys = []
            for m in (children(el, "surfaceMember") + children(el, "surfaceMembers")):
                href = m.get(XLINK_HREF)
                if href:
                    self.note("by-reference surfaceMember %s not supported" % href)
                for c in elements(m):
                    if is_geometry(c):
                        polys.extend(self._sub(c, ctx).polygons)
            return polys
        if name in ("Polygon", "PolygonPatch"):
            return [self._polygon_rings(el, ctx)]
        patches = child(el, "patches")
        if patches is None:
            return []
        return [self._polygon_rings(p, ctx) for p in elements(patches)]

    def _polygon_rings(self, el, ctx):
        rings = []
        for role in ("exterior", "interior"):
            for r in children(el, role):
                for ring in elements(r):
                    rings.append(self._ring_segs(ring, ctx))
        return rings

    def _ring_segs(self, ring, ctx):
        if local(ring.tag) == "LinearRing":
            return [Seg("linear", self._point_items(ring, ctx))]
        segs = []
        for m in children(ring, "curveMember"):
            for line in self._curve_member_lines(m, ctx):
                segs.extend(line)
        return segs


def _angle(el):
    if el is None or text(el) is None:
        raise GeometryParseError("missing arc angle")
    uom = (el.get("uom") or "deg").strip().lower()
    factor = _ANGLE_UOM.get(uom)
    if factor is None:
        raise GeometryParseError("unsupported angle unit %r" % uom)
    return float(text(el)) * factor


def _reverse_seg(seg):
    if seg.kind in ("linear", "geodesic", "arcstring"):
        return Seg(seg.kind, list(reversed(seg.points)))
    if seg.kind == "arc":
        return Seg("arc", seg.points, seg.radius, seg.end, seg.start)
    if seg.kind == "arc3":
        return Seg("arc3", list(reversed(seg.points)))
    return seg
