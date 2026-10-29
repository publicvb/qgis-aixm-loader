"""Flatten AIXM property trees into attribute-table columns.

Simple values become columns; values with a unit get a companion
``<name>_uom`` column; xlinks become ``<name>_uuid`` (plus ``<name>_label``
with the target's designator/name when the dataset can resolve it); nested
objects and repeated properties are kept as JSON text so nothing is lost.
"""
import json

from .geometry import Geom
from .model import AixmObject, Measure, Nil, Ref


def to_jsonable(value):
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Measure):
        return {"value": value.value, "uom": value.uom}
    if isinstance(value, Nil):
        return None if not value.reason else {"nil": value.reason}
    if isinstance(value, Ref):
        d = {"href": value.href}
        if value.title:
            d["title"] = value.title
        return d
    if isinstance(value, AixmObject):
        d = {"_type": value.type}
        d.update({k: to_jsonable(v) for k, v in value.props.items()})
        return d
    if isinstance(value, Geom):
        return {"_geometry": value.kind}
    return value


def _number(text):
    try:
        f = float(text)
    except (TypeError, ValueError):
        return text
    return int(f) if f.is_integer() and "." not in str(text) else f


def flatten(props, prefix="", label_lookup=None):
    """Return an ordered dict of column -> scalar (str, int, float or None)."""
    row = {}
    for name, value in props.items():
        col = prefix + name
        if isinstance(value, Geom):
            continue
        if isinstance(value, Nil):
            row.setdefault(col, None)
        elif isinstance(value, str):
            row[col] = value
        elif isinstance(value, Measure):
            row[col] = _number(value.value)
            row[col + "_uom"] = value.uom
        elif isinstance(value, Ref):
            row[col + "_uuid"] = value.key if value.kind == "uuid" else value.href
            if label_lookup:
                row[col + "_label"] = label_lookup(value)
        elif isinstance(value, list) and all(isinstance(v, Ref) for v in value):
            row[col + "_uuid"] = ";".join(v.key if v.kind == "uuid" else v.href for v in value)
            if label_lookup:
                labels = [label_lookup(v) or "" for v in value]
                row[col + "_label"] = ";".join(labels) if any(labels) else None
        elif isinstance(value, list) and all(isinstance(v, (Geom, Nil)) for v in value):
            continue
        else:
            row[col] = json.dumps(to_jsonable(value), ensure_ascii=False)
    return row


def feature_label(state):
    """Short human label of a feature: designator, name or similar."""
    p = state.props
    for key in ("designator", "name", "designatorICAO", "locationIndicatorICAO", "identifier"):
        v = p.get(key)
        if isinstance(v, str) and v:
            return v
    route = route_designator(p)
    return route or None


def route_designator(p):
    parts = [p.get(k) for k in ("designatorPrefix", "designatorSecondLetter", "designatorNumber",
                                "multipleIdentifier")]
    parts = [x for x in parts if isinstance(x, str)]
    return "".join(parts) if parts else None
