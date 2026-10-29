"""Namespace-agnostic helpers for AIXM / GML elements.

AIXM 5.1, 5.1.1 and 5.2 differ in namespace URIs but share local element
names, so the parser matches on local names and only records the version.
"""
import re

GML_NS = "http://www.opengis.net/gml/3.2"
GML_ID = "{%s}id" % GML_NS
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
XLINK_TITLE = "{http://www.w3.org/1999/xlink}title"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

_AIXM_VERSION_RE = re.compile(r"aixm\.aero/schema/(\d+\.\d+(?:\.\d+)?)")
_XPOINTER_RE = re.compile(r"@gml:id\s*=\s*['\"]([^'\"]+)['\"]")


def local(tag):
    """Local name of an element tag ('' for comments / processing instructions)."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def namespace(tag):
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def is_gml(el):
    return namespace(el.tag) == GML_NS


def elements(el):
    """Child elements, skipping comments."""
    return [c for c in el if isinstance(c.tag, str)]


def child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el, name):
    return [c for c in el if local(c.tag) == name]


def text(el):
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def is_nil(el):
    return el.get(XSI_NIL) in ("true", "1")


def aixm_version(ns_uri):
    m = _AIXM_VERSION_RE.search(ns_uri or "")
    return m.group(1) if m else None


def parse_href(href):
    """Classify an xlink:href.

    Returns (kind, key) where kind is 'uuid' (feature identifier), 'gmlid'
    (document-local gml:id) or 'external' (anything else, key = full href).
    """
    h = href.strip()
    low = h.lower()
    if low.startswith("urn:uuid:"):
        return "uuid", h[9:].lower()
    if low.startswith("#uuid."):
        return "uuid", h[6:].lower()
    if h.startswith("#xpointer("):
        m = _XPOINTER_RE.search(h)
        if m:
            return "gmlid", m.group(1)
    if h.startswith("#"):
        return "gmlid", h[1:]
    # e.g. http://.../features/<uuid> or ...?uuid=<uuid>
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$", h)
    if m and ("uuid" in low or "/" in h):
        return "uuid", m.group(1).lower()
    return "external", h
