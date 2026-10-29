"""Remember how the user styles imported layers.

When the style of a layer read from an AIXM Loader GeoPackage changes, it is
saved (debounced) in two places, keyed by the layer's table name (e.g.
``Airspace``), which is the same in every import:

  * the GeoPackage's standard ``layer_styles`` table (default style), so the
    style travels with the file;
  * ``<QGIS profile>/aixm_loader/styles/<table>.qml``, so it applies to every
    later import on this machine.

When layers are added, the GeoPackage's own style wins, then the profile
style, then the plugin's built-in styling. Categories missing from a saved
categorized style (values new in this import) are added automatically.

Only symbology, labels, rendering and diagrams are remembered.
"""
import os
from contextlib import contextmanager

from osgeo import ogr
from qgis.core import (
    QgsApplication, QgsCategorizedSymbolRenderer, QgsMapLayer, QgsProject, QgsRendererCategory,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtXml import QDomDocument

from ..compat import qenum
from . import store

STYLE_NAME = "aixm_loader"
STYLES_TABLE = "layer_styles"


def style_categories():
    cats = None
    for name in ("Symbology", "Labeling", "Rendering", "Diagrams"):
        c = qenum(QgsMapLayer, "StyleCategory", name)
        cats = c if cats is None else cats | c
    return cats


def layer_source(layer):
    """(gpkg path, table name) of an OGR GeoPackage layer, else (None, None)."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None, None
    parts = layer.source().split("|")
    path = parts[0]
    if not path.lower().endswith(".gpkg"):
        return None, None
    for p in parts[1:]:
        if p.startswith("layername="):
            return path, p[len("layername="):]
    return path, None


def default_profile_dir():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "aixm_loader", "styles")


def gpkg_style(path, table):
    """QML text of the remembered style of ``table`` stored in the GeoPackage, or None."""
    try:
        ds = ogr.Open(path, 0)
    except RuntimeError:
        return None
    lyr = ds.GetLayerByName(STYLES_TABLE) if ds is not None else None
    if lyr is None:
        return None
    lyr.SetAttributeFilter("f_table_name = '%s' AND styleName = '%s'"
                           % (table.replace("'", "''"), STYLE_NAME))
    qml = None
    for f in lyr:
        qml = f.GetField("styleQML") or qml
    ds = None
    return qml


def delete_gpkg_styles(path):
    """Remove the styles this plugin saved in a GeoPackage."""
    try:
        ds = ogr.Open(path, 1)
    except RuntimeError:
        return
    if ds is not None and ds.GetLayerByName(STYLES_TABLE) is not None:
        ds.ExecuteSQL("DELETE FROM %s WHERE styleName = '%s'" % (STYLES_TABLE, STYLE_NAME))
    ds = None


class StyleMemory(QObject):
    """Saves user styles of imported layers and re-applies them to later imports."""

    def __init__(self, profile_dir=None, delay_ms=800, parent=None):
        super().__init__(parent)
        self.profile_dir = profile_dir or default_profile_dir()
        self.delay_ms = delay_ms
        self._tracked = {}  # layer id -> layer
        self._timers = {}  # layer id -> QTimer (pending save)
        self._aixm_paths = set()  # GeoPackages known to be AIXM Loader imports
        self._paused = 0

    # -- remembered styles ---------------------------------------------------
    def profile_qml(self, table):
        return os.path.join(self.profile_dir, "%s.qml" % table)

    def saved_style(self, path, table):
        """QML of the remembered style: the GeoPackage's first, else the profile's."""
        qml = gpkg_style(path, table) if path else None
        if qml:
            return qml
        f = self.profile_qml(table)
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as fh:
                return fh.read()
        return None

    def apply(self, layer, missing_category_symbol=None):
        """Apply the remembered style, if any. Returns True when one was applied.

        missing_category_symbol(layer, value) -> QgsSymbol for values a saved
        categorized style does not know yet.
        """
        path, table = layer_source(layer)
        if not table:
            return False
        qml = self.saved_style(path, table)
        if not qml:
            return False
        doc = QDomDocument()
        if not _set_content(doc, qml):
            return False
        with self.paused():
            ok, _ = layer.importNamedStyle(doc, style_categories())
            if ok:
                complete_categories(layer, missing_category_symbol)
                layer.triggerRepaint()
        return ok

    def save(self, layer):
        """Save the layer's style to its GeoPackage and to the profile."""
        path, table = layer_source(layer)
        if not table:
            return
        doc = QDomDocument()
        err = layer.exportNamedStyle(doc, categories=style_categories())
        if err:
            return
        os.makedirs(self.profile_dir, exist_ok=True)
        with open(self.profile_qml(table), "w", encoding="utf-8") as fh:
            fh.write(doc.toString())
        # saveStyleToDatabaseV2 is the QGIS 4 name; 3.34 only has the original
        save_db = getattr(layer, "saveStyleToDatabaseV2", None) or layer.saveStyleToDatabase
        save_db(STYLE_NAME, "Remembered by AIXM Loader", True, "", style_categories())

    def reset(self, gpkg_paths=()):
        """Forget all remembered styles (profile files and the given GeoPackages)."""
        self.cancel_pending()
        if os.path.isdir(self.profile_dir):
            for name in os.listdir(self.profile_dir):
                if name.lower().endswith(".qml"):
                    os.remove(os.path.join(self.profile_dir, name))
        for p in gpkg_paths:
            delete_gpkg_styles(p)

    # -- tracking ------------------------------------------------------------
    @contextmanager
    def paused(self):
        """Style changes made inside this block are not remembered."""
        self._paused += 1
        try:
            yield
        finally:
            self._paused -= 1

    def is_import_layer(self, layer):
        path, table = layer_source(layer)
        if not table:
            return False
        key = os.path.normcase(os.path.abspath(path))
        if key not in self._aixm_paths:
            if not store.inspect(path).is_aixm:
                return False
            self._aixm_paths.add(key)
        return True

    def attach(self, project=None):
        """Follow layers being added to / removed from the project (and those already in it)."""
        project = project or QgsProject.instance()
        project.layersAdded.connect(self.layers_added)
        project.layersWillBeRemoved.connect(self.layers_will_be_removed)
        self.layers_added(project.mapLayers().values())

    def detach(self, project=None):
        project = project or QgsProject.instance()
        self.flush()
        project.layersAdded.disconnect(self.layers_added)
        project.layersWillBeRemoved.disconnect(self.layers_will_be_removed)

    def layers_added(self, layers):
        for layer in layers:
            if self.is_import_layer(layer):
                self.track(layer)

    def layers_will_be_removed(self, layer_ids):
        """Keep the last change of layers leaving the project (they are still intact here)."""
        for lid in layer_ids:
            if lid in self._timers:
                self._save_pending(lid)
            self._tracked.pop(lid, None)

    def track(self, layer):
        lid = layer.id()
        if lid in self._tracked:
            return
        self._tracked[lid] = layer
        for sig in ("styleChanged", "rendererChanged", "opacityChanged"):
            s = getattr(layer, sig, None)
            if s is not None:
                s.connect(lambda *a, lid=lid: self._changed(lid))
        # emitted from the destructor: the layer can no longer be saved, only forgotten
        layer.willBeDeleted.connect(lambda lid=lid: self._forget(lid))

    def _changed(self, lid):
        if self._paused or lid not in self._tracked:
            return
        timer = self._timers.get(lid)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda lid=lid: self._save_pending(lid))
            self._timers[lid] = timer
        timer.start(self.delay_ms)

    def _save_pending(self, lid):
        timer = self._timers.pop(lid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        layer = self._tracked.get(lid)
        if layer is not None:
            self.save(layer)

    def flush(self):
        """Save pending changes now (e.g. before the layers are removed for a re-import)."""
        for lid in list(self._timers):
            self._save_pending(lid)

    def cancel_pending(self):
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()

    @property
    def pending(self):
        return set(self._timers)

    def _forget(self, lid):
        timer = self._timers.pop(lid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._tracked.pop(lid, None)


def complete_categories(layer, symbol_for=None):
    """Add categories for attribute values a categorized style does not cover yet."""
    renderer = layer.renderer()
    if not isinstance(renderer, QgsCategorizedSymbolRenderer):
        return
    idx = layer.fields().indexOf(renderer.classAttribute())
    if idx < 0:
        return
    known = {c.value() for c in renderer.categories()}
    for value in sorted(v for v in layer.uniqueValues(idx) if v not in (None, "") and v not in known):
        symbol = symbol_for(layer, value) if symbol_for else None
        if symbol is None:
            base = renderer.sourceSymbol() or (renderer.categories()[0].symbol() if renderer.categories() else None)
            if base is None:
                continue
            symbol = base.clone()
        renderer.addCategory(QgsRendererCategory(value, symbol, str(value)))


def _set_content(doc, text):
    res = doc.setContent(text)
    # PyQt5: (bool, str, int, int); PyQt6 / Qt6: ParseResult or bool
    if isinstance(res, tuple):
        return bool(res[0])
    return bool(res)
