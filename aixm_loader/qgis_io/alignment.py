"""Detect airspace boundaries that should coincide but do not.

Gaps and sliver overlaps between airspaces come from their defining points
disagreeing, so the check works on published vertices (segment control
points, followed border vertices - not densified or computed arc points):

  vertex_mismatch   a vertex of A lies within the tolerance of a vertex of B
                    but not on it: the same point coded differently
  vertex_off_edge   a vertex of A lies within the tolerance of B's boundary
                    but not on it: a junction that misses the neighbour's edge

Boundaries that merely run close to each other (tangents, edges diverging
from a shared vertex) are not reported. Distances are measured in an
azimuthal equidistant projection (metres).
"""
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
)

# offsets below this (m) are within the rounding of published coordinates
# (0.1 arc-second is ~3 m in latitude, ~2 m in longitude at 50 deg)
MIN_OFFSET_M = 2.0

_WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class Finding:
    def __init__(self, kind, vertex, geometry, offset_m):
        self.kind = kind  # vertex_mismatch | vertex_off_edge
        self.vertex = vertex  # (lon, lat) of the published vertex, WGS84
        self.geometry = geometry  # QgsGeometry line vertex -> neighbour, WGS84
        self.offset_m = offset_m


class Projection:
    """WGS84 <-> local azimuthal equidistant around a centre point."""

    def __init__(self, lon, lat):
        local = QgsCoordinateReferenceSystem.fromProj(
            "+proj=aeqd +lat_0=%f +lon_0=%f +datum=WGS84 +units=m +no_defs" % (lat, lon))
        ctx = QgsProject.instance().transformContext()
        self.fwd = QgsCoordinateTransform(_WGS84, local, ctx)
        self.back = QgsCoordinateTransform(local, _WGS84, ctx)

    @classmethod
    def around(cls, *geoms):
        rect = None
        for g in geoms:
            if g is None or g.isEmpty():
                continue
            if rect is None:
                rect = g.boundingBox()
            else:
                rect.combineExtentWith(g.boundingBox())
        c = rect.center() if rect is not None else QgsPointXY(0, 0)
        return cls(c.x(), c.y())


class Shape:
    """An airspace projected to metres, with its boundary and published vertices."""

    def __init__(self, geom, vertices, projection):
        self.proj = projection
        g = QgsGeometry(geom)
        g.transform(projection.fwd)
        self.boundary = QgsGeometry(g.constGet().boundary())
        r = g.boundingBox()
        self.bbox = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        self.vertices = []  # (x, y, original (lon, lat)) - plain floats for fast loops
        seen = set()
        for v in vertices:
            key = (round(v[0], 9), round(v[1], 9))
            if key in seen:
                continue
            seen.add(key)
            p = projection.fwd.transform(QgsPointXY(*v))
            self.vertices.append((p.x(), p.y(), v))
        self._engine = None

    def distance(self, x, y):
        if self._engine is None:
            self._engine = QgsGeometry.createGeometryEngine(self.boundary.constGet())
            self._engine.prepareGeometry()
        return self._engine.distance(QgsGeometry.fromPointXY(QgsPointXY(x, y)).constGet())


def _within(x, y, box, pad):
    return box[0] - pad <= x <= box[2] + pad and box[1] - pad <= y <= box[3] + pad


class AlignmentChecker:
    def __init__(self, tolerance_m):
        self.tol = float(tolerance_m)

    def shape(self, geom, vertices, projection):
        return Shape(geom, vertices, projection)

    def compare_wgs(self, a, a_vertices, b, b_vertices):
        """Findings of a's vertices against b and b's against a (WGS84 inputs).

        Returns (findings_for_a, findings_for_b).
        """
        if a is None or b is None or a.isEmpty() or b.isEmpty() or self.tol <= 0:
            return [], []
        proj = Projection.around(a, b)
        sa, sb = Shape(a, a_vertices, proj), Shape(b, b_vertices, proj)
        return self.vertices_against(sa, sb), self.vertices_against(sb, sa)

    def vertices_against(self, a, b):
        """Published vertices of shape a that miss shape b by less than the tolerance."""
        tol = self.tol
        if tol <= 0:
            return []
        ab, bb = a.bbox, b.bbox
        if ab[0] > bb[2] + tol or bb[0] > ab[2] + tol or ab[1] > bb[3] + tol or bb[1] > ab[3] + tol:
            return []
        tol2 = tol * tol
        b_near = [w for w in b.vertices if _within(w[0], w[1], ab, tol)]
        out = []
        for x, y, lonlat in a.vertices:
            if not _within(x, y, bb, tol):
                continue
            d_edge = b.distance(x, y)
            if d_edge <= MIN_OFFSET_M or d_edge > tol:
                continue
            best, best_d2 = None, tol2
            for wx, wy, _ in b_near:
                d2 = (wx - x) ** 2 + (wy - y) ** 2
                if d2 <= best_d2:
                    best, best_d2 = (wx, wy), d2
            p = QgsPointXY(x, y)
            if best is not None and best_d2 > MIN_OFFSET_M ** 2:
                target, kind, offset = QgsPointXY(*best), "vertex_mismatch", best_d2 ** 0.5
            else:
                target = b.boundary.nearestPoint(QgsGeometry.fromPointXY(p)).asPoint()
                kind, offset = "vertex_off_edge", d_edge
            line = QgsGeometry.fromPolylineXY([p, target])
            line.transform(a.proj.back)
            out.append(Finding(kind, lonlat, line, offset))
        return out
