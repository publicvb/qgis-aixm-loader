"""In-memory representation of parsed AIXM content.

Property values held in ``props`` dictionaries are one of:
  str        simple value
  Measure    value with a unit of measurement (uom attribute)
  Nil        xsi:nil property, with optional nilReason
  Ref        xlink reference to another feature / object
  AixmObject nested AIXM object (e.g. AirspaceGeometryComponent)
  Geom       geometry (see core.geometry)
  list       of any of the above, for repeated properties
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Measure:
    value: str
    uom: Optional[str]

    def as_float(self):
        try:
            return float(self.value)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Nil:
    reason: Optional[str] = None


@dataclass(frozen=True)
class Ref:
    kind: str  # 'uuid' | 'gmlid' | 'external'
    key: str
    title: Optional[str] = None

    @property
    def href(self):
        if self.kind == "uuid":
            return "urn:uuid:" + self.key
        if self.kind == "gmlid":
            return "#" + self.key
        return self.key


@dataclass
class AixmObject:
    type: str
    gml_id: Optional[str]
    props: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TimeSlice:
    type: str
    gml_id: Optional[str]
    interpretation: Optional[str]
    valid_begin: Optional[str]
    valid_end: Optional[str]
    sequence: Optional[int]
    correction: Optional[int]
    lifetime_begin: Optional[str]
    lifetime_end: Optional[str]
    props: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Feature:
    type: str
    uuid: str
    gml_id: Optional[str]
    source: str
    timeslices: List[TimeSlice] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)  # parse notes / errors
    xml: Optional[bytes] = field(default=None, repr=False)  # zlib-compressed source XML, if kept


@dataclass
class FeatureState:
    """A feature as valid at one instant: the merged result of its timeslices."""

    feature: Feature
    props: Dict[str, Any]
    interpretation: Optional[str]
    valid_begin: Optional[str]
    valid_end: Optional[str]
    sequence: Optional[int]
    correction: Optional[int]
    lifetime_begin: Optional[str]
    lifetime_end: Optional[str]

    @property
    def type(self):
        return self.feature.type

    @property
    def uuid(self):
        return self.feature.uuid


def add_prop(props, name, value):
    """Store a property value, turning repeated properties into lists."""
    if name in props:
        existing = props[name]
        if isinstance(existing, list):
            existing.append(value)
        else:
            props[name] = [existing, value]
    else:
        props[name] = value


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
