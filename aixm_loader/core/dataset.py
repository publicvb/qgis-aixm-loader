"""A set of AIXM files loaded together, so xlinks resolve across files."""
import os

from .flatten import feature_label
from .geometry import Geom, Seg
from .model import AixmObject, Ref, as_list
from .parser import AixmParser
from .resolve import GeometryResolver
from .timeslice import feature_state


class AixmDataset:
    def __init__(self, tolerance_m=50.0):
        self.features = {}  # uuid -> Feature
        self.records = []  # (source, uuid, type, compressed xml) when kept
        self.versions = set()
        self.sources = []  # source file names, in load order
        self.warnings = []
        self.states = {}  # uuid -> FeatureState, after snapshot()
        self.resolver = GeometryResolver(self.point_lookup, self.curve_lookup, self.ref_label,
                                         tolerance_m=tolerance_m)
        self._point_cache = {}
        self._curve_cache = {}

    # -- loading -------------------------------------------------------
    def load(self, paths, progress=None, is_cancelled=None, keep_xml=False):
        """Parse files. progress(fraction 0..1, message) is optional.

        With keep_xml, ``self.records`` collects (source, uuid, type, xml) of
        every parsed feature so the import can be stored and extended later.
        """
        for i, path in enumerate(paths):
            def file_progress(f, i=i, path=path):
                if progress:
                    progress((i + f) / len(paths), path)
            parser = AixmParser()  # gml:ids are only unique within one document
            features = parser.iter_features(path, file_progress, is_cancelled, keep_xml)
            self._add_document(parser, features, os.path.basename(path))
            if is_cancelled and is_cancelled():
                break

    def load_records(self, records, is_cancelled=None):
        """Load features stored by an earlier import: [(source, uuid, type, xml)]."""
        by_source = {}
        for rec in records:
            by_source.setdefault(rec[0], []).append(rec)
        for source, recs in by_source.items():
            parser = AixmParser()  # one parser per original document for its gml:ids
            features = (parser.parse_stored(r[3], source) for r in recs)
            self._add_document(parser, features, source)
            if is_cancelled and is_cancelled():
                break

    def _add_document(self, parser, features, source):
        parsed = []
        for feature in features:
            parsed.append(feature)
            if feature.xml is not None:
                self.records.append((source, feature.uuid, feature.type, feature.xml))
                feature.xml = None
            existing = self.features.get(feature.uuid)
            if existing is None:
                self.features[feature.uuid] = feature
            else:  # same feature in several files: merge timeslices
                existing.timeslices.extend(feature.timeslices)
                existing.issues.extend(feature.issues)
        _inline_local_refs(parsed, parser.gml)
        self.versions |= parser.versions
        self.warnings.extend(parser.warnings)
        if source not in self.sources:
            self.sources.append(source)

    def snapshot(self, at=None):
        """Compute feature states valid at ``at`` (aware datetime or None = latest)."""
        self.states = {}
        for uuid, f in self.features.items():
            st = feature_state(f, at)
            if st is not None:
                self.states[uuid] = st
        self._point_cache.clear()
        self._curve_cache.clear()
        return self.states

    def by_type(self):
        out = {}
        for st in self.states.values():
            out.setdefault(st.type, []).append(st)
        return out

    # -- lookups used by the resolver -----------------------------------
    def state(self, ref):
        if isinstance(ref, Ref) and ref.kind == "uuid":
            return self.states.get(ref.key)
        return None

    def ref_label(self, ref):
        st = self.state(ref)
        return feature_label(st) if st is not None else None

    def point_lookup(self, ref):
        if ref in self._point_cache:
            return self._point_cache[ref]
        xy = None
        st = self.state(ref)
        if st is not None:
            xy = self.feature_point(st)
        self._point_cache[ref] = xy
        return xy

    def feature_point(self, state):
        """Representative point of a point feature (location, ARP, ...)."""
        for _, geom, _ in iter_geometries(state.props):
            if geom.kind == "point":
                res = self.resolver.resolve(geom)
                if res.points:
                    return res.points[0]
        return None

    def curve_lookup(self, ref):
        if ref in self._curve_cache:
            return self._curve_cache[ref]
        self._curve_cache[ref] = None  # guards against reference cycles
        coords = None
        st = self.state(ref)
        if st is not None:
            for _, geom, _ in iter_geometries(st.props):
                if geom.kind == "line":
                    coords = _join(self.resolver.resolve(geom).lines)
                    break
        self._curve_cache[ref] = coords
        return coords


def _join(lines):
    out = []
    for line in lines:
        for xy in line:
            if not out or out[-1] != xy:
                out.append(xy)
    return out or None


def iter_geometries(props, path=(), owner=None, skip=()):
    """Yield (property path, Geom, owning props dict) for every geometry in props.

    ``owner`` is the props dict of the object directly holding the geometry
    property, so callers can attach that object's attributes (e.g. a
    VerticalStructurePart's height to its location).
    """
    for name, value in props.items():
        if not path and name in skip:
            continue
        for v in as_list(value):
            if isinstance(v, Geom):
                yield path + (name,), v, props
            elif isinstance(v, AixmObject):
                yield from iter_geometries(v.props, path + (name,), v.props)


# -- document-local xlinks (href="#gml-id") -------------------------------
def _inline_local_refs(features, gml):
    """Replace '#id' xlinks to geometries, curves and points defined in the same document.

    GML semantics: a by-reference geometry property means that geometry, a
    by-reference curveMember means the whole referenced curve.
    """
    for f in features:
        for ts in f.timeslices:
            _inline_props(ts.props, gml, f.issues)


def _inline_props(props, gml, issues):
    for name, value in list(props.items()):
        if isinstance(value, list):
            props[name] = [_inline_value(v, gml, issues) for v in value]
        else:
            props[name] = _inline_value(value, gml, issues)


def _inline_value(v, gml, issues):
    if isinstance(v, Ref) and v.kind == "gmlid" and v.key in gml.geoms:
        v = gml.geoms[v.key]
    if isinstance(v, AixmObject):
        _inline_props(v.props, gml, issues)
    elif isinstance(v, Geom):
        _inline_geom(v, gml, issues)
    return v


def _local_point(p, gml):
    if isinstance(p, Ref) and p.kind == "gmlid":
        g = gml.geoms.get(p.key)
        if g is not None and g.kind == "point" and g.points and not isinstance(g.points[0], Ref):
            return g.points[0]
    return p


def _inline_segs(segs, gml, depth=0):
    out = []
    for s in segs:
        if s.kind == "ref" and s.ref.kind == "gmlid" and s.ref.key in gml.curves and depth < 8:
            out.extend(_inline_segs(gml.curves[s.ref.key], gml, depth + 1))
        else:
            if s.points:
                s = Seg(s.kind, [_local_point(p, gml) for p in s.points], s.radius, s.start, s.end, s.ref)
            out.append(s)
    return out


def _inline_geom(g, gml, issues):
    g.points = [_local_point(p, gml) for p in g.points]
    g.lines = [_inline_segs(line, gml) for line in g.lines]
    g.polygons = [[_inline_segs(ring, gml) for ring in poly] for poly in g.polygons]
