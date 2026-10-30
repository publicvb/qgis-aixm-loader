"""Streaming AIXM 5.x parser (BasicMessage, WFS feature collections, ...).

Features are recognised structurally (an element with gml:identifier and
timeSlice children) rather than by container name, so any message wrapper
works. Each feature subtree is released after it has been converted.
"""
import os
import xml.etree.ElementTree as ET
import zlib

from .geometry import GmlParser, is_geometry
from .model import AixmObject, Feature, Measure, Nil, Ref, TimeSlice, add_prop
from .xmlutil import (
    GML_ID, XLINK_HREF, XLINK_TITLE, aixm_version, child, elements, is_gml, is_nil, local,
    namespace, parse_href, text,
)

# timeslice children handled explicitly rather than as properties
_TS_META = {"validTime", "interpretation", "sequenceNumber", "correctionNumber", "featureLifetime"}
# gml elements without AIXM meaning
_SKIP_GML = {"boundedBy", "identifier", "metaDataProperty", "description", "descriptionReference"}


# AIXM never uses a DTD; refusing documents that declare one rules out entity
# expansion attacks (billion laughs, external entities) in user-supplied files
_DTD_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")
_MARKER_TAIL = max(len(m) for m in _DTD_MARKERS) - 1


def check_no_dtd(data):
    for marker in _DTD_MARKERS:
        if marker in data:
            raise ValueError("XML with a DTD or entity declarations (%s) is not accepted"
                             % marker.decode("ascii"))


class _ProgressReader:
    """File wrapper that reports the fraction of bytes consumed and refuses DTDs."""

    def __init__(self, fh, size, callback):
        self._fh, self._size, self._cb = fh, max(size, 1), callback
        self._pos = 0
        self._tail = b""  # end of the previous chunk: a marker may straddle two reads

    def read(self, n=-1):
        data = self._fh.read(n)
        check_no_dtd(self._tail + data)
        self._tail = data[-_MARKER_TAIL:]
        self._pos += len(data)
        if self._cb:
            self._cb(self._pos / self._size)
        return data


class AixmParser:
    """Parses one document; gml:id registries are document-local (``self.gml``)."""

    def __init__(self, warn=None):
        self.warnings = []
        self._warn_cb = warn
        self.gml = GmlParser()
        self.versions = set()

    def _warn(self, msg):
        self.warnings.append(msg)
        if self._warn_cb:
            self._warn_cb(msg)

    # -- streaming -----------------------------------------------------
    def _feature_elements(self, path, progress=None, is_cancelled=None):
        """Yield each feature element of a document; it is released afterwards."""
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            reader = _ProgressReader(fh, size, progress)
            stack = []
            for event, el in ET.iterparse(reader, events=("start", "end")):
                if event == "start":
                    stack.append(el)
                    v = aixm_version(namespace(el.tag))
                    if v:
                        self.versions.add(v)
                    continue
                stack.pop()
                if not _looks_like_feature(el):
                    continue
                if is_cancelled and is_cancelled():
                    return
                yield el
                if stack:
                    stack[-1].remove(el)
                el.clear()

    def iter_features(self, path, progress=None, is_cancelled=None, keep_xml=False):
        """Parse features; with keep_xml each Feature carries its compressed source XML."""
        source = os.path.basename(path)
        for el in self._feature_elements(path, progress, is_cancelled):
            try:
                feature = self.parse_feature(el, source)
            except (ValueError, TypeError, AttributeError) as e:
                self._warn("%s %s skipped: %s" % (local(el.tag), el.get(GML_ID), e))
                continue
            if keep_xml:
                feature.xml = zlib.compress(ET.tostring(el), 6)
            yield feature

    def scan(self, path, progress=None, is_cancelled=None):
        """{feature type: count} of a document, without converting the features."""
        counts = {}
        for el in self._feature_elements(path, progress, is_cancelled):
            name = local(el.tag)
            counts[name] = counts.get(name, 0) + 1
        return counts

    def parse_stored(self, blob, source):
        """Feature from XML stored by iter_features(keep_xml=True)."""
        data = zlib.decompress(blob)
        check_no_dtd(data)  # written by iter_features, but the GeoPackage may come from elsewhere
        el = ET.fromstring(data)
        for e in el.iter():
            v = aixm_version(namespace(e.tag))
            if v:
                self.versions.add(v)
                break
        feature = self.parse_feature(el, source)
        feature.xml = blob
        return feature

    # -- features ------------------------------------------------------
    def parse_feature(self, el, source=""):
        ident = text(child(el, "identifier"))
        gml_id = el.get(GML_ID)
        feature = Feature(type=local(el.tag), uuid="", gml_id=gml_id, source=source)
        if ident:
            feature.uuid = ident.lower()
        elif gml_id and gml_id.lower().startswith("uuid."):
            feature.uuid = gml_id[5:].lower()
            feature.issues.append("no gml:identifier; uuid taken from gml:id")
        else:
            feature.uuid = "%s#%s" % (source, gml_id)
            feature.issues.append("no gml:identifier; feature cannot be referenced by uuid")
        self.gml.notes = feature.issues
        for prop in elements(el):
            if local(prop.tag) != "timeSlice":
                continue
            for ts in elements(prop):
                feature.timeslices.append(self.parse_timeslice(ts))
        if not feature.timeslices:
            feature.issues.append("feature without timeslices")
        return feature

    def parse_timeslice(self, el):
        begin, end = _time_period(child(el, "validTime"))
        lbegin, lend = _time_period(child(el, "featureLifetime"))
        ts = TimeSlice(
            type=local(el.tag),
            gml_id=el.get(GML_ID),
            interpretation=text(child(el, "interpretation")),
            valid_begin=begin,
            valid_end=end,
            sequence=_int(text(child(el, "sequenceNumber"))),
            correction=_int(text(child(el, "correctionNumber"))),
            lifetime_begin=lbegin,
            lifetime_end=lend,
        )
        for prop in elements(el):
            name = local(prop.tag)
            if name in _TS_META or (is_gml(prop) and name in _SKIP_GML):
                continue
            add_prop(ts.props, name, self.parse_property(prop))
        return ts

    def parse_property(self, el, srs=None):
        if is_nil(el):
            return Nil(el.get("nilReason"))
        href = el.get(XLINK_HREF)
        if href:
            kind, key = parse_href(href)
            return Ref(kind, key, el.get(XLINK_TITLE))
        kids = elements(el)
        if not kids:
            value = text(el) or ""
            uom = el.get("uom")
            return Measure(value, uom) if uom is not None else value
        if len(kids) == 1:
            return self.parse_object(kids[0], srs)
        # property without an object wrapper: treat the element itself as object
        return self.parse_object(el, srs)

    def parse_object(self, el, srs=None):
        srs = el.get("srsName") or srs
        if is_geometry(el):
            return self.gml.parse(el, srs, self._geometry_props(el, srs))
        obj = AixmObject(type=local(el.tag), gml_id=el.get(GML_ID))
        for prop in elements(el):
            name = local(prop.tag)
            if is_gml(prop) and name in _SKIP_GML:
                continue
            add_prop(obj.props, name, self.parse_property(prop, srs))
        return obj

    def _geometry_props(self, el, srs):
        """AIXM properties (elevation, accuracy, ...) attached to a geometry."""
        props = {}
        for prop in elements(el):
            if is_gml(prop):
                continue
            add_prop(props, local(prop.tag), self.parse_property(prop, srs))
        return props


def _looks_like_feature(el):
    """AIXM features carry timeSlice properties (and normally gml:identifier)."""
    has_id = el.get(GML_ID) is not None
    for c in el:
        name = local(c.tag)
        if name == "identifier":
            has_id = True
        elif name == "timeSlice" and elements(c):
            return has_id or local(elements(c)[0].tag).endswith("TimeSlice")
    return False


def _time_period(prop):
    """(begin, end) ISO strings of a validTime / featureLifetime property."""
    if prop is None:
        return None, None
    for t in elements(prop):
        name = local(t.tag)
        if name == "TimePeriod":
            return _time_pos(_first(t, "beginPosition", "begin")), _time_pos(_first(t, "endPosition", "end"))
        if name == "TimeInstant":
            p = _time_pos(child(t, "timePosition"))
            return p, p
    return None, None


def _first(el, *names):
    # explicit None checks: an Element without children is falsy
    for n in names:
        c = child(el, n)
        if c is not None:
            return c
    return None


def _time_pos(el):
    if el is None:
        return None
    if local(el.tag) in ("begin", "end"):  # TimeInstantProperty
        inst = elements(el)
        return _time_pos(child(inst[0], "timePosition")) if inst else None
    if el.get("indeterminatePosition") in ("unknown", "now"):
        return None
    return text(el)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
