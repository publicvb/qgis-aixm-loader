"""Build attribute rows and QgsGeometry for every output layer.

Generic rule: every geometry property found anywhere in a feature becomes a
row in layer ``<FeatureType>_<property path>``. Features with special
semantics get dedicated builders:
  Airspace      geometry composed from geometryComponents (BASE / UNION /
                SUBTR / INTERS, contributor airspaces, border following),
                checked for misaligned boundaries
  RouteSegment  curveExtent, or a geodesic line between start and end points
Feature types without any geometry are written as attribute-only tables.

Every problem found on the way is collected as an ``Issue`` (with a location
where one is known) so the user can be told about it.
"""
from collections import OrderedDict

from qgis.core import (
    Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
    QgsSpatialIndex, QgsWkbTypes,
)

from ..core import geodesy
from ..core.dataset import iter_geometries
from ..core.flatten import feature_label, flatten
from ..core.geometry import Geom
from ..core.model import AixmObject, Measure, Ref, as_list
from ..core.resolve import GeomIssue
from .alignment import AlignmentChecker, Projection

WGS84 = "EPSG:4326"


class LayerData:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind  # 'point' | 'line' | 'polygon' | None
        self.rows = []  # [(OrderedDict attrs, QgsGeometry | None)]


class Issue:
    """One problem found during the import, for the issues table and layers."""

    def __init__(self, state, layer, kind, message, severity="warning", geometry=None,
                 size_m=None, related=None):
        self.feature_type = state.type if state is not None else None
        self.uuid = state.uuid if state is not None else None
        self.label = feature_label(state) if state is not None else None
        self.layer = layer
        self.kind = kind
        self.message = message
        self.severity = severity
        self.geometry = geometry  # QgsGeometry (point or line, WGS84) or None
        self.size_m = size_m
        self.related_uuid, self.related_label = related or (None, None)

    @classmethod
    def from_geom_issue(cls, state, layer, gi, prefix=""):
        geom = QgsGeometry.fromPointXY(QgsPointXY(*gi.location)) if gi.location else None
        return cls(state, layer, gi.kind, prefix + gi.message, gi.severity, geom, gi.size_m)


def to_qgs(resolved, issues=None):
    """QgsGeometry (multi-type for lines / polygons) from a core Resolved.

    Invalid polygons are repaired; each self-intersection is appended to
    ``issues`` as a located GeomIssue.
    """
    if resolved.kind == "point":
        if not resolved.points:
            return None
        if len(resolved.points) == 1:
            return QgsGeometry.fromPointXY(QgsPointXY(*resolved.points[0]))
        return QgsGeometry.fromMultiPointXY([QgsPointXY(*p) for p in resolved.points])
    if resolved.kind == "line":
        if not resolved.lines:
            return None
        return QgsGeometry.fromMultiPolylineXY([[QgsPointXY(*p) for p in line] for line in resolved.lines])
    if not resolved.polygons:
        return None
    g = QgsGeometry.fromMultiPolygonXY(
        [[[QgsPointXY(*p) for p in ring] for ring in poly] for poly in resolved.polygons])
    if issues is not None and not g.isGeosValid():
        for err in g.validateGeometry():
            loc = (err.where().x(), err.where().y()) if err.hasWhere() else None
            issues.append(GeomIssue("invalid_geometry", "%s (repaired)" % err.what(), loc))
    return _valid(g)


def _valid(g):
    """Valid polygonal geometry; drops points / lines left over by makeValid or overlay ops."""
    if g is None or g.isEmpty():
        return g
    if not g.isGeosValid():
        g = g.makeValid()
    if g.type() != Qgis.GeometryType.Polygon or QgsWkbTypes.flatType(g.wkbType()) == Qgis.WkbType.GeometryCollection:
        polys = [p for p in g.asGeometryCollection() if p.type() == Qgis.GeometryType.Polygon]
        g = QgsGeometry.collectGeometry(polys) if polys else QgsGeometry()
    return g


def _multi(g):
    if g is not None and not g.isEmpty() and not g.isMultipart():
        g.convertToMultiType()
    return g


def base_columns(state):
    f = state.feature
    return OrderedDict([
        ("uuid", f.uuid),
        ("gml_id", f.gml_id),
        ("label", feature_label(state)),
        ("interpretation", state.interpretation),
        ("valid_begin", state.valid_begin),
        ("valid_end", state.valid_end),
        ("sequence", state.sequence),
        ("correction", state.correction),
        ("lifetime_begin", state.lifetime_begin),
        ("lifetime_end", state.lifetime_end),
        ("source_file", f.source),
    ])


def limit_text(value, uom, ref):
    """Readable vertical limit, e.g. FL245, 5000 FT MSL, GND, UNL."""
    if value is None:
        return None
    v = str(value)
    if v.upper() in ("UNL", "GND", "FLOOR", "CEILING"):
        return v.upper()
    if (uom or "").upper() == "FL":
        return "FL%s" % v
    if ref == "SFC" and v in ("0", "0.0"):
        return "GND"
    return " ".join(x for x in (v, uom, ref) if x)


class LayerBuilder:
    def __init__(self, dataset, feedback=None, tolerance_m=50.0, check_neighbours=True, classes=None):
        self.ds = dataset
        self.classes = set(classes) if classes is not None else None  # None = all feature types
        self.output_feature_count = 0
        self.layers = OrderedDict()
        self.issues = []
        self.feedback = feedback
        self.checker = AlignmentChecker(tolerance_m)
        self.check_neighbours = check_neighbours
        self._airspace_cache = {}
        self._airspace_active = set()
        self._contributors = {}  # airspace uuid -> set of direct contributor uuids
        self._siblings = set()  # frozenset pairs already compared inside a composition
        self._neighbour_issues = {}  # airspace uuid -> number of misaligned stretches

    # -- helpers ---------------------------------------------------------
    def label_of(self, ref):
        st = self.ds.state(ref)
        return feature_label(st) if st is not None else None

    def _layer(self, name, kind):
        lyr = self.layers.get(name)
        if lyr is None:
            lyr = self.layers[name] = LayerData(name, kind)
        elif lyr.kind != kind:
            return self._layer("%s_%s" % (name, kind), kind)
        return lyr

    def _cancelled(self):
        return self.feedback is not None and self.feedback.isCanceled()

    def _progress(self, p):
        if self.feedback is not None:
            self.feedback.setProgress(p)

    # -- main ------------------------------------------------------------
    def build(self, include_tables=True):
        """Layers for the selected classes; other features only serve as xlink targets."""
        for f in self.ds.features.values():
            if self.classes is not None and f.type not in self.classes:
                continue
            st = self.ds.states.get(f.uuid)
            for msg in f.issues:
                self.issues.append(Issue(st, f.type, "parse_note", msg, "info") if st is not None
                                   else _orphan_issue(f, msg))
        by_type = self.ds.by_type()
        if self.classes is not None:
            by_type = {t: s for t, s in by_type.items() if t in self.classes}
        self.output_feature_count = sum(len(s) for s in by_type.values())
        total = max(1, self.output_feature_count)
        done = 0
        spatial_types = set()
        for ftype in sorted(by_type):
            states = by_type[ftype]
            if self._cancelled():
                break
            if ftype == "Airspace":
                self._build_airspaces(states, 100.0 * done / total, 100.0 * len(states) / total)
                spatial_types.add(ftype)
            elif ftype == "RouteSegment":
                self._build_route_segments(states)
                spatial_types.add(ftype)
            elif self._build_generic(ftype, states):
                spatial_types.add(ftype)
            done += len(states)
            self._progress(100.0 * done / total)
        if include_tables:
            for ftype in sorted(set(by_type) - spatial_types):
                lyr = self._layer(ftype, None)
                for st in by_type[ftype]:
                    lyr.rows.append((self._attrs(st), None))
        return self.layers

    def _attrs(self, state, skip=()):
        row = base_columns(state)
        props = {k: v for k, v in state.props.items() if k not in skip}
        for k, v in flatten(props, label_lookup=self.label_of).items():
            if k not in row:
                row[k] = v
        return row

    # -- generic -----------------------------------------------------------
    def _build_generic(self, ftype, states):
        found = False
        for st in states:
            attrs = None
            counters = {}
            for path, geom, owner in iter_geometries(st.props):
                found = True
                name = "%s_%s" % (ftype, "_".join(path))
                res = self.ds.resolver.resolve(geom)
                geom_issues = list(res.issues)
                g = to_qgs(res, geom_issues)
                if geom.kind in ("line", "polygon"):
                    g = _multi(g)
                if attrs is None:
                    attrs = self._attrs(st)
                row = OrderedDict(attrs)
                idx = counters[name] = counters.get(name, 0) + 1
                row["geom_index"] = idx
                if len(path) > 1:  # attributes of the object owning the geometry
                    prefix = "_".join(path[:-1]) + "_"
                    for k, v in flatten(owner, prefix, self.label_of).items():
                        row[k] = v
                for k, v in flatten(geom.props, "geom_", self.label_of).items():
                    row[k] = v
                row["geom_issues"] = "; ".join(map(str, geom_issues)) or None
                for gi in geom_issues:
                    self.issues.append(Issue.from_geom_issue(st, name, gi))
                self._layer(name, geom.kind).rows.append((row, g))
        return found

    # -- airspaces ---------------------------------------------------------
    def _build_airspaces(self, states, progress_start, progress_span):
        results = []
        for i, st in enumerate(states):
            if self._cancelled():
                return
            geom, issues, limits, vertices = self.airspace(st.uuid)
            results.append((st, geom, list(issues), limits, vertices))
            self._progress(progress_start + 0.5 * progress_span * (i + 1) / len(states))
        if self.check_neighbours:
            self._check_neighbours(results, progress_start + 0.5 * progress_span, 0.5 * progress_span)
        lyr = self._layer("Airspace", "polygon")
        for st, geom, issues, limits, _ in results:
            row = self._attrs(st)
            for k, v in limits.items():
                row[k] = v
            errors = [i for i in issues if i.severity == "error"]
            row["geom_complete"] = 0 if (errors or geom is None or geom.isEmpty()) else 1
            n_neighbour = self._neighbour_issues.get(st.uuid, 0)
            row["issue_count"] = len(issues) + n_neighbour
            texts = [i.message for i in issues]
            if n_neighbour:
                texts.append("%d misaligned stretch(es) with neighbouring airspaces (see %s)"
                             % (n_neighbour, "aixm_issues"))
            row["geom_issues"] = "; ".join(texts) or None
            self.issues.extend(issues)
            lyr.rows.append((row, _multi(geom)))

    def airspace(self, uuid):
        """(QgsGeometry | None, [Issue], limits dict, published vertices) of an airspace, memoised."""
        if uuid in self._airspace_cache:
            return self._airspace_cache[uuid]
        st = self.ds.states.get(uuid)
        if st is None:
            return None, [], {}, []
        if uuid in self._airspace_active:
            return None, [Issue(st, "Airspace", "unresolved_reference",
                                "circular airspace dependency", "error")], {}, []
        self._airspace_active.add(uuid)
        try:
            result = self._compose(st)
        finally:
            self._airspace_active.discard(uuid)
        self._airspace_cache[uuid] = result
        return result

    def _compose(self, st):
        comps = [c for c in as_list(st.props.get("geometryComponent")) if isinstance(c, AixmObject)]
        comps.sort(key=lambda c: _int(c.props.get("operationSequence")))
        geom, issues, limits, vertices = None, [], {}, []
        contributors = []
        if not comps:
            issues.append(Issue(st, "Airspace", "geometry_error", "airspace without geometryComponent",
                                "error"))
        for c in comps:
            op = c.props.get("operation") if isinstance(c.props.get("operation"), str) else "BASE"
            vol = c.props.get("theAirspaceVolume")
            if not isinstance(vol, AixmObject):
                issues.append(Issue(st, "Airspace", "geometry_error", "%s component without volume" % op,
                                    "error"))
                continue
            g, vol_limits, contributor, g_vertices = self._volume(st, vol, issues)
            if not limits or op == "BASE":
                limits = vol_limits if vol_limits else limits
            if g is None or g.isEmpty():
                issues.append(Issue(st, "Airspace", "geometry_error", "%s component has no geometry" % op,
                                    "error"))
                continue
            if contributor:
                contributors.append(contributor)
            if geom is not None and op in ("BASE", "UNION", "SUBTR", "SUBTRACT", "INTERS", "INTERSECT"):
                self._check_operands(st, op, geom, vertices, g, g_vertices, contributor, issues)
            vertices = vertices + g_vertices
            if geom is None:
                geom = g
            elif op in ("BASE", "UNION"):
                geom = geom.combine(g)
            elif op in ("SUBTR", "SUBTRACT"):
                geom = geom.difference(g)
            elif op in ("INTERS", "INTERSECT"):
                geom = geom.intersection(g)
            else:
                issues.append(Issue(st, "Airspace", "geometry_error", "unsupported operation %s" % op,
                                    "error"))
            geom = _valid(geom)
            if geom is None or geom.isEmpty():
                issues.append(Issue(st, "Airspace", "geometry_error",
                                    "geometry empty after %s operation" % op, "error"))
        uuids = {c[0] for c in contributors}
        self._contributors[st.uuid] = uuids
        for a in uuids:
            for b in uuids:
                if a < b:
                    self._siblings.add(frozenset((a, b)))
        return geom, issues, limits, vertices

    def _check_operands(self, st, op, acc, acc_vertices, g, g_vertices, contributor, issues):
        """Misaligned vertices between the accumulated geometry and the next operand."""
        what = "%s contributor %s" % (op, contributor[1]) if contributor else "%s component" % op
        for_g, for_acc = self.checker.compare_wgs(g, g_vertices, acc, acc_vertices)
        for f in for_g:
            issues.append(_finding_issue(st, f, "%s: vertex" % what, "the other components", contributor))
        for f in for_acc:
            issues.append(_finding_issue(st, f, "vertex of the components before %s" % what,
                                         contributor[1] if contributor else "it", contributor))

    def _volume(self, st, vol, issues):
        """(geometry, limits, (contributor uuid, label) or None, vertices) of an AirspaceVolume."""
        p = vol.props
        limits = _limits(p)
        hp = p.get("horizontalProjection")
        if isinstance(hp, Geom):
            res = self.ds.resolver.resolve(hp)
            gis = list(res.issues)
            g = to_qgs(res, gis)
            issues.extend(Issue.from_geom_issue(st, "Airspace", gi) for gi in gis)
            return g, limits, None, res.vertices
        if isinstance(hp, Ref):
            issues.append(Issue(st, "Airspace", "unresolved_reference",
                                "horizontalProjection %s not found" % hp.href, "error"))
            return None, limits, None, []
        dep = p.get("contributorAirspace")
        if isinstance(dep, AixmObject):
            ref = dep.props.get("theAirspace")
            if not isinstance(ref, Ref):
                issues.append(Issue(st, "Airspace", "geometry_error",
                                    "contributorAirspace without theAirspace", "error"))
                return None, limits, None, []
            label = self.label_of(ref) or ref.key
            if self.ds.state(ref) is None:
                issues.append(Issue(st, "Airspace", "unresolved_reference",
                                    "contributor airspace %s not in dataset" % ref.href, "error"))
                return None, limits, None, []
            g, sub_issues, sub_limits, sub_vertices = self.airspace(ref.key)
            for i in sub_issues:  # contributor problems make this airspace incomplete too
                if i.severity == "error":
                    issues.append(Issue(st, "Airspace", i.kind, "via %s: %s" % (label, i.message),
                                        "error", i.geometry, i.size_m, (ref.key, label)))
            if dep.props.get("dependency") == "FULL_GEOMETRY" or not _has_limits(limits):
                limits = sub_limits or limits
            return (QgsGeometry(g) if g is not None else None), limits, (ref.key, label), sub_vertices
        centre = p.get("centreline")
        width = p.get("width")
        if isinstance(centre, Geom) and isinstance(width, Measure):
            res = self.ds.resolver.resolve(centre)
            issues.extend(Issue.from_geom_issue(st, "Airspace", gi) for gi in res.issues)
            line = to_qgs(res)
            try:
                half = geodesy.to_metres(width.value, width.uom) / 2
            except ValueError as e:
                issues.append(Issue(st, "Airspace", "geometry_error", str(e), "error"))
                return None, limits, None, []
            return buffer_metres(line, half), limits, None, []
        return None, limits, None, []

    def _ancestors(self, uuid, seen=None):
        """All airspaces uuid is (transitively) built from."""
        seen = set() if seen is None else seen
        for c in self._contributors.get(uuid, ()):
            if c not in seen:
                seen.add(c)
                self._ancestors(c, seen)
        return seen

    def _check_neighbours(self, results, progress_start, progress_span):
        """Misaligned vertices between independent airspaces.

        Each misaligned vertex is reported once per airspace, listing every
        neighbour it misses (stacked sectors often share the same boundary).
        """
        valid = [r for r in results if r[1] is not None and not r[1].isEmpty()]
        if not valid:
            return
        proj = Projection.around(*[r[1] for r in valid])
        # airspaces stacked vertically often share one horizontal shape: compare each
        # distinct shape once and hand the findings to every member
        groups = OrderedDict()  # shape key -> [result index]
        for n, (st, geom, issues, _, vertices) in enumerate(results):
            if geom is None or geom.isEmpty():
                continue
            key = (bytes(geom.asWkb()), tuple(sorted(set(vertices))))
            groups.setdefault(key, []).append(n)
        index = QgsSpatialIndex()
        reps, shapes = [], []
        for g, (key, members) in enumerate(groups.items()):
            st, geom, _, _, vertices = results[members[0]]
            reps.append(members)
            shapes.append(self.checker.shape(geom, vertices, proj))
            index.addFeature(g, geom.boundingBox())
        pad = self.checker.tol / 30000.0  # tolerance in degrees for the bbox search, generous
        related = {r[0].uuid: self._ancestors(r[0].uuid) for r in results}
        raw = []  # (finding, owner result indexes, other result indexes)

        def excluded(a, b):
            return (b.uuid in related[a.uuid] or a.uuid in related[b.uuid]
                    or frozenset((a.uuid, b.uuid)) in self._siblings)

        for g in range(len(reps)):
            if self._cancelled():
                return
            rect = results[reps[g][0]][1].boundingBox().buffered(pad)
            for h in index.intersects(rect):
                if h <= g:  # each pair of shapes once, both directions below
                    continue
                pairs = [(n, m) for n in reps[g] for m in reps[h]
                         if not excluded(results[n][0], results[m][0])]
                if not pairs:
                    continue
                in_g = sorted({n for n, _ in pairs})
                in_h = sorted({m for _, m in pairs})
                raw.extend((f, in_g, in_h) for f in self.checker.vertices_against(shapes[g], shapes[h]))
                raw.extend((f, in_h, in_g) for f in self.checker.vertices_against(shapes[h], shapes[g]))
            self._progress(progress_start + progress_span * (g + 1) / max(1, len(reps)))

        for members in _cluster(raw, proj, CLUSTER_LINK_M):
            owners = OrderedDict((results[n][0].uuid, results[n][0]) for _, ns, _ in members for n in ns)
            others = OrderedDict((results[m][0].uuid, results[m][0]) for _, _, ms in members for m in ms)
            per_vertex = OrderedDict()  # one offset line per vertex: the largest
            for f, _, _ in members:
                key = (round(f.vertex[0], 7), round(f.vertex[1], 7))
                if key not in per_vertex or f.offset_m > per_vertex[key].offset_m:
                    per_vertex[key] = f
            offsets = [f.offset_m for f in per_vertex.values()]
            owner_names = _names(owners.values())
            other_names = _names(others.values())
            kinds = {f.kind for f in per_vertex.values()}
            issue = Issue(
                next(iter(owners.values())), "Airspace", "misaligned_boundary",
                "%d vertex(es) of %s miss %s by %s (%s): gap or overlap slivers" % (
                    len(per_vertex), owner_names, other_names, _range(offsets),
                    " and ".join(sorted(KIND_TEXT[k] for k in kinds))),
                "warning",
                QgsGeometry.collectGeometry([f.geometry for f in per_vertex.values()]),
                max(offsets), (next(iter(others)), other_names))
            issue.label = owner_names
            self.issues.append(issue)
            for uuid in set(owners) | set(others):
                self._neighbour_issues[uuid] = self._neighbour_issues.get(uuid, 0) + 1

    # -- route segments ------------------------------------------------------
    def _build_route_segments(self, states):
        lyr = self._layer("RouteSegment", "line")
        for st in states:
            issues = []
            g = None
            curve = st.props.get("curveExtent")
            if isinstance(curve, Geom):
                res = self.ds.resolver.resolve(curve)
                issues.extend(res.issues)
                g = to_qgs(res)
            if g is None:
                a = self._segment_point(st.props.get("start"), issues)
                b = self._segment_point(st.props.get("end"), issues)
                if a is not None and b is not None:
                    pts = geodesy.densify_great_circle([a, b], self.ds.resolver.densify_m)
                    g = QgsGeometry.fromMultiPolylineXY([[QgsPointXY(*p) for p in pts]])
            row = self._attrs(st)
            for name in ("start", "end"):
                row[name + "_point"] = self._segment_point_label(st.props.get(name))
            row["geom_issues"] = "; ".join(map(str, issues)) or None
            for gi in issues:
                self.issues.append(Issue.from_geom_issue(st, "RouteSegment", gi))
            lyr.rows.append((row, _multi(g)))

    def _segment_point(self, obj, issues):
        if not isinstance(obj, AixmObject):
            issues.append(GeomIssue("geometry_error", "segment end point missing", severity="error"))
            return None
        for name, v in obj.props.items():
            if not name.startswith("pointChoice"):
                continue
            if isinstance(v, Ref):
                xy = self.ds.point_lookup(v)
                if xy is None:
                    issues.append(GeomIssue("unresolved_reference", "unresolved point %s" % v.href,
                                            severity="error"))
                return xy
            if isinstance(v, Geom):
                res = self.ds.resolver.resolve(v)
                issues.extend(res.issues)
                return res.points[0] if res.points else None
        issues.append(GeomIssue("geometry_error", "segment end point without pointChoice", severity="error"))
        return None

    def _segment_point_label(self, obj):
        if isinstance(obj, AixmObject):
            for name, v in obj.props.items():
                if name.startswith("pointChoice") and isinstance(v, Ref):
                    return self.label_of(v)
        return None


# misaligned vertices closer than this (m) to each other form one reported stretch
CLUSTER_LINK_M = 2000.0
KIND_TEXT = {"vertex_mismatch": "vertices coded differently",
             "vertex_off_edge": "vertices off the neighbour's edge"}


def _names(states, limit=6):
    names = sorted({feature_label(s) or s.uuid for s in states})
    if len(names) > limit:
        return "%s and %d more" % (", ".join(names[:limit]), len(names) - limit)
    return ", ".join(names)


def _range(values):
    lo, hi = min(values), max(values)
    return "%.0f m" % hi if hi - lo < 1 else "%.0f-%.0f m" % (lo, hi)


def _cluster(raw, proj, link_m):
    """Group findings whose vertices lie within link_m of each other (grid + union-find)."""
    pts = [proj.fwd.transform(QgsPointXY(*f.vertex)) for f, _, _ in raw]
    parent = list(range(len(raw)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid = {}
    for i, p in enumerate(pts):
        grid.setdefault((int(p.x() // link_m), int(p.y() // link_m)), []).append(i)
    for (cx, cy), cell in grid.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    for i in cell:
                        if i < j and (pts[i].x() - pts[j].x()) ** 2 + (pts[i].y() - pts[j].y()) ** 2 <= link_m ** 2:
                            parent[find(i)] = find(j)
    groups = OrderedDict()
    for i in range(len(raw)):
        groups.setdefault(find(i), []).append(raw[i])
    return list(groups.values())


def _finding_issue(st, f, subject, other, related):
    if f.kind == "vertex_mismatch":
        text = "%s differs by %.1f m from a vertex of %s (gap or overlap sliver)" % (subject, f.offset_m, other)
    else:
        text = "%s lies %.1f m off the boundary of %s (gap or overlap sliver)" % (subject, f.offset_m, other)
    return Issue(st, "Airspace", f.kind, text, "warning", f.geometry, f.offset_m, related)


def _orphan_issue(feature, msg):
    i = Issue(None, feature.type, "parse_note", msg, "info")
    i.feature_type, i.uuid = feature.type, feature.uuid
    return i


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _limits(p):
    out = OrderedDict()
    for key, col in (("upperLimit", "upper"), ("lowerLimit", "lower"),
                     ("maximumLimit", "maximum"), ("minimumLimit", "minimum")):
        v = p.get(key)
        ref = p.get(key + "Reference")
        ref = ref if isinstance(ref, str) else None
        if isinstance(v, Measure):
            out[col + "_limit"] = v.value
            out[col + "_limit_uom"] = v.uom
            out[col + "_limit_ref"] = ref
            out[col + "_limit_text"] = limit_text(v.value, v.uom, ref)
        elif col in ("upper", "lower"):
            out[col + "_limit"] = None
            out[col + "_limit_uom"] = None
            out[col + "_limit_ref"] = None
            out[col + "_limit_text"] = None
    return out


def _has_limits(limits):
    return bool(limits.get("upper_limit") or limits.get("lower_limit"))


def buffer_metres(geom, distance_m):
    """Buffer a WGS84 geometry by a distance in metres (local equidistant projection)."""
    if geom is None or geom.isEmpty():
        return None
    c = geom.centroid().asPoint()
    local = QgsCoordinateReferenceSystem.fromProj(
        "+proj=aeqd +lat_0=%f +lon_0=%f +datum=WGS84 +units=m +no_defs" % (c.y(), c.x()))
    wgs = QgsCoordinateReferenceSystem(WGS84)
    ctx = QgsProject.instance().transformContext()
    g = QgsGeometry(geom)
    g.transform(QgsCoordinateTransform(wgs, local, ctx))
    g = g.buffer(distance_m, 16)
    g.transform(QgsCoordinateTransform(local, wgs, ctx))
    return g
