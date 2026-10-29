"""Add the layers of an imported GeoPackage to the current QGIS project, styled."""
import hashlib
import os

from qgis.core import (
    Qgis, QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsLayerTree, QgsLineSymbol, QgsMarkerSymbol,
    QgsPalLayerSettings, QgsProject, QgsRendererCategory, QgsTextFormat, QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

AIRSPACE_COLOURS = {
    "CTR": "220,40,40", "CTA": "40,90,220", "TMA": "40,90,220", "ATZ": "240,140,0",
    "TIZ": "240,140,0", "P": "150,0,0", "R": "230,60,120", "D": "255,120,0",
    "TSA": "130,60,180", "TRA": "130,60,180", "FIR": "90,90,90", "UIR": "90,90,90",
    "SECTOR": "0,140,140", "CBA": "130,60,180", "AWY": "60,130,60",
}
POINT_SHAPES = {
    "DesignatedPoint": ("triangle", "30,30,30", 2.2),
    "Navaid": ("hexagon", "0,90,170", 3.0),
    "AirportHeliport": ("circle", "200,0,120", 3.2),
    "VerticalStructure": ("triangle", "220,0,0", 1.6),
    "RunwayCentrelinePoint": ("diamond", "90,90,90", 1.6),
}
NO_LABELS = ("VerticalStructure", "RunwayCentrelinePoint")
ORDER = {"point": 0, "line": 1, "polygon": 2}


def _hash_colour(value):
    h = hashlib.md5(str(value).encode("utf-8")).digest()
    return "%d,%d,%d" % (60 + h[0] % 160, 60 + h[1] % 160, 60 + h[2] % 160)


def _airspace_renderer(layer):
    idx = layer.fields().indexOf("type")
    if idx < 0:
        return None
    cats = []
    for value in sorted(v for v in layer.uniqueValues(idx) if v not in (None, "")):
        rgb = AIRSPACE_COLOURS.get(str(value).split("-")[0], _hash_colour(value))
        sym = QgsFillSymbol.createSimple({
            "color": rgb + ",30", "outline_color": rgb + ",255", "outline_width": "0.4",
        })
        cats.append(QgsRendererCategory(value, sym, str(value)))
    return QgsCategorizedSymbolRenderer("type", cats) if cats else None


def _label(layer, field="label"):
    if layer.fields().indexOf(field) < 0:
        return
    s = QgsPalLayerSettings()
    s.fieldName = field
    s.enabled = True
    fmt = QgsTextFormat()
    fmt.setSize(8)
    s.setFormat(fmt)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(s))
    layer.setLabelsEnabled(True)


def style_layer(layer, name, kind):
    ftype = name.split("_", 1)[0]
    if kind == "polygon":
        renderer = _airspace_renderer(layer) if name == "Airspace" else None
        if renderer is not None:
            layer.setRenderer(renderer)
        else:
            rgb = _hash_colour(name)
            layer.renderer().setSymbol(QgsFillSymbol.createSimple({
                "color": rgb + ",50", "outline_color": rgb + ",255", "outline_width": "0.3"}))
    elif kind == "line":
        colour, width = ("40,40,160", "0.5") if ftype == "RouteSegment" else (_hash_colour(name), "0.4")
        if ftype == "GeoBorder":
            colour, width = "120,120,120", "0.6"
        layer.renderer().setSymbol(QgsLineSymbol.createSimple({"line_color": colour, "line_width": width}))
    elif kind == "point":
        shape, colour, size = POINT_SHAPES.get(ftype, ("circle", _hash_colour(name), 2.0))
        layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({
            "name": shape, "color": colour, "size": str(size), "outline_color": "255,255,255",
            "outline_width": "0.2"}))
        if ftype not in NO_LABELS:
            _label(layer)


def _gpkg_of(layer):
    return os.path.normcase(os.path.abspath(layer.source().split("|")[0]))


def project_imports():
    """AIXM Loader GeoPackages that can be added to and are loaded in the project, top-most first."""
    from . import store

    out, seen = [], set()
    for node in QgsProject.instance().layerTreeRoot().findLayers():
        lyr = node.layer()
        if lyr is None or lyr.providerType() != "ogr":
            continue
        path = lyr.source().split("|")[0]
        key = os.path.normcase(os.path.abspath(path))
        if key in seen or not path.lower().endswith(".gpkg"):
            continue
        seen.add(key)
        if store.inspect(path).appendable:
            out.append(path)
    return out


def remove_from_project(gpkg_path):
    """Remove all layers read from gpkg_path; returns the name of their group (or None).

    Used before re-importing into the same GeoPackage: its layers are rebuilt.
    """
    target = os.path.normcase(os.path.abspath(gpkg_path))
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group_name, ids = None, []
    for node in root.findLayers():
        lyr = node.layer()
        if lyr is None or lyr.providerType() != "ogr" or _gpkg_of(lyr) != target:
            continue
        ids.append(lyr.id())
        parent = node.parent()
        while parent is not None and parent.parent() is not None and parent.parent() is not root:
            parent = parent.parent()  # the top-level group of the import
        if group_name is None and parent is not None and parent is not root:
            group_name = parent.name()
    if not ids:
        return None
    project.removeMapLayers(ids)
    group = root.findGroup(group_name) if group_name else None
    if group is not None and _prune(group):
        root.removeChildNode(group)
    return group_name or os.path.splitext(os.path.basename(gpkg_path))[0]


def _prune(group):
    """Remove empty sub-groups; True when the group itself ended up empty."""
    for child in list(group.children()):
        if QgsLayerTree.isGroup(child) and _prune(child):
            group.removeChildNode(child)
    return not group.children()


def style_issue_layer(layer, kind):
    if kind == "point":
        layer.renderer().setSymbol(QgsMarkerSymbol.createSimple({
            "name": "cross2", "color": "230,0,0", "size": "4", "outline_color": "230,0,0",
            "outline_width": "0.6"}))
    elif kind == "line":
        layer.renderer().setSymbol(QgsLineSymbol.createSimple({"line_color": "230,0,0", "line_width": "1.2"}))


def add_to_project(gpkg_path, written, group_name=None, include_tables=True):
    """Add layers [(name, kind, count)] from gpkg_path under a new layer-tree group.

    Returns the added layers; issue layers go into an "Issues" sub-group on top.
    """
    from .importer import ISSUE_LAYERS, ISSUES_TABLE

    project = QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.insertGroup(0, group_name or os.path.splitext(os.path.basename(gpkg_path))[0])
    added = []
    issue_layers = [w for w in written if w[0] in ISSUE_LAYERS or w[0] == ISSUES_TABLE]
    if any(c for name, _, c in issue_layers):
        issues_group = group.addGroup("Issues")
        for name, kind, count in issue_layers:
            if not count:
                continue
            layer = QgsVectorLayer("%s|layername=%s" % (gpkg_path, name), name, "ogr")
            if not layer.isValid():
                continue
            style_issue_layer(layer, kind)
            project.addMapLayer(layer, False)
            issues_group.addLayer(layer)
            added.append(layer)
    written = [w for w in written if w not in issue_layers]
    tables = None
    spatial = sorted((w for w in written if w[1]), key=lambda w: (ORDER[w[1]], w[0] != "Airspace", w[0]))
    for name, kind, _ in spatial + [w for w in written if not w[1]]:
        if kind is None and not include_tables:
            continue
        layer = QgsVectorLayer("%s|layername=%s" % (gpkg_path, name), name, "ogr")
        if not layer.isValid():
            continue
        if kind:
            style_layer(layer, name, kind)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        else:
            if tables is None:
                tables = group.addGroup("Tables (no geometry)")
                tables.setExpanded(False)
            project.addMapLayer(layer, False)
            tables.addLayer(layer)
        added.append(layer)
    # large point layers start hidden to keep the first render fast
    for node in group.findLayers():
        lyr = node.layer()
        if lyr.geometryType() == Qgis.GeometryType.Point and lyr.featureCount() > 2000:
            node.setItemVisibilityChecked(False)
    return added
