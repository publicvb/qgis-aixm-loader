"""Remembered user styles (offscreen Qt)."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    QgsApplication, QgsCategorizedSymbolRenderer, QgsFillSymbol, QgsProject, QgsRendererCategory,
    QgsSingleSymbolRenderer,
)

_app = QgsApplication.instance()
if _app is None:
    _app = QgsApplication([], True)
    _app.initQgis()

from aixm_loader.qgis_io import store  # noqa: E402
from aixm_loader.qgis_io.importer import run_import  # noqa: E402
from aixm_loader.qgis_io.project_loader import add_to_project, restyle_project_layers  # noqa: E402
from aixm_loader.qgis_io.styles import StyleMemory, gpkg_style  # noqa: E402
from tests import aixm_samples as S  # noqa: E402

UA = "cccccccc-0000-0000-0000-00000000000a"
UB = "cccccccc-0000-0000-0000-00000000000b"
SQ_A = [(15.0, 50.0), (15.1, 50.0), (15.1, 50.1), (15.0, 50.1), (15.0, 50.0)]
SQ_B = [(15.2, 50.0), (15.3, 50.0), (15.3, 50.1), (15.2, 50.1), (15.2, 50.0)]


def make_import(*members, gpkg=None, append=False):
    path = S.write(S.message(*members))
    gpkg = gpkg or os.path.join(tempfile.mkdtemp(), "imp.gpkg")
    try:
        res = run_import([path], gpkg, check_neighbours=False, append=append)
    finally:
        S.cleanup(path)
    return gpkg, res


def red_fill():
    return QgsFillSymbol.createSimple({"color": "255,0,0,255", "outline_color": "0,0,0"})


class StyleMemoryTests(unittest.TestCase):
    def setUp(self):
        self.profile = tempfile.mkdtemp()
        self.styles = StyleMemory(profile_dir=self.profile, delay_ms=0)
        self.styles.attach()

    def tearDown(self):
        self.styles.detach()
        QgsProject.instance().clear()

    def load(self, gpkg, res, group="imp"):
        added = add_to_project(gpkg, res.layers, group, styles=self.styles)
        return next(lyr for lyr in added if lyr.name() == "Airspace")

    def test_builtin_styling_is_not_remembered(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A))
        layer = self.load(gpkg, res)
        self.assertEqual(layer.renderer().type(), "categorizedSymbol")  # built-in style
        self.assertEqual(self.styles.pending, set())
        self.assertEqual(os.listdir(self.profile), [])
        self.assertIsNone(gpkg_style(gpkg, "Airspace"))

    def test_user_style_is_saved_in_profile_and_geopackage(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A))
        layer = self.load(gpkg, res)
        layer.setRenderer(QgsSingleSymbolRenderer(red_fill()))
        layer.setOpacity(0.5)
        layer.emitStyleChanged()
        self.assertIn(layer.id(), self.styles.pending)
        self.styles.flush()
        self.assertTrue(os.path.isfile(os.path.join(self.profile, "Airspace.qml")))
        self.assertIn("singleSymbol", gpkg_style(gpkg, "Airspace"))

        # a separate, new import picks the style up from the profile
        gpkg2, res2 = make_import(S.polygon_airspace(UB, "B", SQ_B))
        layer2 = self.load(gpkg2, res2, "second")
        self.assertEqual(layer2.renderer().type(), "singleSymbol")
        self.assertEqual(layer2.renderer().symbol().color().red(), 255)
        self.assertAlmostEqual(layer2.opacity(), 0.5)

    def test_geopackage_style_survives_reimport_and_wins_over_profile(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A))
        layer = self.load(gpkg, res)
        layer.setRenderer(QgsSingleSymbolRenderer(red_fill()))
        layer.emitStyleChanged()
        self.styles.flush()
        # the profile style is changed afterwards (e.g. styling another import)
        with open(os.path.join(self.profile, "Airspace.qml"), "w", encoding="utf-8") as fh:
            fh.write("<qgis><renderer-v2 type=\"nullSymbol\"/></qgis>")
        QgsProject.instance().clear()
        # additive import into the same GeoPackage rebuilds the Airspace table
        gpkg, res = make_import(S.polygon_airspace(UB, "B", SQ_B), gpkg=gpkg, append=True)
        self.assertIn("layer_styles", store.inspect(gpkg).layer_names)
        layer = self.load(gpkg, res)
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual(layer.renderer().type(), "singleSymbol")  # the GeoPackage's style

    def test_new_category_values_are_added_to_remembered_style(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A, type_="CTR"))
        layer = self.load(gpkg, res)
        renderer = QgsCategorizedSymbolRenderer("type", [QgsRendererCategory("CTR", red_fill(), "CTR")])
        layer.setRenderer(renderer)
        layer.emitStyleChanged()
        self.styles.flush()
        gpkg2, res2 = make_import(S.polygon_airspace(UA, "A", SQ_A, type_="CTR"),
                                  S.polygon_airspace(UB, "B", SQ_B, type_="TMA"))
        layer2 = self.load(gpkg2, res2, "second")
        cats = {c.value(): c for c in layer2.renderer().categories()}
        self.assertEqual(set(cats), {"CTR", "TMA"})
        self.assertEqual(cats["CTR"].symbol().color().red(), 255)  # remembered
        self.assertEqual(cats["TMA"].symbol().color().blue(), 220)  # plugin colour for TMA

    def test_removed_layer_keeps_last_change_and_reset_forgets(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A))
        layer = self.load(gpkg, res)
        layer.setRenderer(QgsSingleSymbolRenderer(red_fill()))
        layer.emitStyleChanged()
        QgsProject.instance().removeMapLayer(layer.id())  # pending change saved on removal
        self.assertTrue(os.path.isfile(os.path.join(self.profile, "Airspace.qml")))
        self.assertIsNotNone(gpkg_style(gpkg, "Airspace"))
        self.styles.reset([gpkg])
        self.assertEqual(os.listdir(self.profile), [])
        self.assertIsNone(gpkg_style(gpkg, "Airspace"))
        layer = self.load(gpkg, res)
        self.assertEqual(layer.renderer().type(), "categorizedSymbol")  # built-in again

    def test_restyle_project_layers_restores_builtin_without_saving(self):
        gpkg, res = make_import(S.polygon_airspace(UA, "A", SQ_A))
        layer = self.load(gpkg, res)
        layer.setRenderer(QgsSingleSymbolRenderer(red_fill()))
        self.assertEqual(self.styles.pending, {layer.id()})  # a user change
        self.styles.reset([gpkg])  # as the plugin's reset action: forget, then restyle paused
        with self.styles.paused():
            restyle_project_layers([gpkg])
        self.assertEqual(layer.renderer().type(), "categorizedSymbol")
        self.assertEqual(self.styles.pending, set())
        self.assertEqual(os.listdir(self.profile), [])


if __name__ == "__main__":
    unittest.main()
