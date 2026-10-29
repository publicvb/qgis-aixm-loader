"""End-to-end tests; run with the QGIS Python (scripts/run_tests.ps1)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    Qgis, QgsApplication, QgsGeometry, QgsPointXY, QgsVectorLayer, QgsWkbTypes,
)

_app = QgsApplication.instance()
if _app is None:
    _app = QgsApplication([], False)
    _app.initQgis()

from aixm_loader.core.dataset import AixmDataset, iter_geometries  # noqa: E402
from aixm_loader.qgis_io.builder import to_qgs  # noqa: E402
from aixm_loader.qgis_io import store  # noqa: E402
from aixm_loader.qgis_io.importer import ISSUES_TABLE, run_import  # noqa: E402

SAMPLES = os.path.join(ROOT, "assets", "sample_xml_files")
LKAA = os.path.join(SAMPLES, "LKAA_FULL_20230713.xml")
LKTB = os.path.join(SAMPLES, "LKTB_AD_FULL_20250627.xml")
BORDERS = os.path.join(SAMPLES, "Geoborder_260806_dataset.xml")
EAD_SDD = os.path.join(SAMPLES, "BD_2024-10-03_83ed6087-99b3-402e-982d-58d115d2b146.aixm5.xml")


def layer(gpkg, name):
    lyr = QgsVectorLayer("%s|layername=%s" % (gpkg, name), name, "ogr")
    assert lyr.isValid(), name
    return lyr


@unittest.skipUnless(os.path.isfile(LKAA), "sample data not available")
class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.gpkg = os.path.join(cls.tmp, "lkaa.gpkg")
        cls.result = run_import([LKAA, BORDERS, LKTB], cls.gpkg)

    def test_layers_written(self):
        names = {n for n, _, _ in self.result.layers}
        for expected in ("Airspace", "RouteSegment", "DesignatedPoint_location", "Navaid_location",
                         "AirportHeliport_ARP", "GeoBorder_border", "Runway"):
            self.assertIn(expected, names)

    def test_raw_airspace_polygons_valid(self):
        """Arc direction check: wrongly swept arcs make self-intersecting rings."""
        ds = AixmDataset()
        ds.load([LKAA, BORDERS])
        ds.snapshot()
        invalid = []
        for st in ds.states.values():
            if st.type != "Airspace":
                continue
            for _, g, _ in iter_geometries(st.props):
                res = ds.resolver.resolve(g)
                raw = QgsGeometry.fromMultiPolygonXY(
                    [[[QgsPointXY(*p) for p in r] for r in poly] for poly in res.polygons])
                if not raw.isGeosValid():
                    invalid.append(st.props.get("designator"))
        self.assertEqual(invalid, [])

    def test_airspace_layer_only_polygons(self):
        lyr = layer(self.gpkg, "Airspace")
        types = {f.geometry().wkbType() for f in lyr.getFeatures() if f.hasGeometry()}
        self.assertEqual({QgsWkbTypes.flatType(t) for t in types}, {Qgis.WkbType.MultiPolygon})

    def test_every_airspace_has_geometry_and_is_complete(self):
        lyr = layer(self.gpkg, "Airspace")
        self.assertEqual(lyr.featureCount(), 350)
        missing = [f["designator"] for f in lyr.getFeatures() if not f.hasGeometry()]
        incomplete = [(f["designator"], f["geom_issues"]) for f in lyr.getFeatures()
                      if f["geom_complete"] != 1]
        self.assertEqual(missing, [])
        self.assertEqual(incomplete, [])

    def test_composed_airspace_is_union_of_contributors(self):
        lyr = layer(self.gpkg, "Airspace")
        feats = {f["uuid"]: f for f in lyr.getFeatures()}
        ds = AixmDataset()
        ds.load([LKAA])
        ds.snapshot()
        checked = 0
        for st in ds.states.values():
            if st.type != "Airspace":
                continue
            comps = st.props.get("geometryComponent")
            if not isinstance(comps, list) or any(
                    c.props.get("operation") != "UNION" for c in comps[1:]):
                continue
            g = feats[st.uuid].geometry()
            for c in comps:
                dep = c.props["theAirspaceVolume"].props.get("contributorAirspace")
                if dep is None:
                    continue
                sub = feats[dep.props["theAirspace"].key].geometry()
                # every contributor lies inside the union (tolerance for rounding)
                self.assertLess(sub.difference(g.buffer(1e-6, 4)).area(), 1e-8)
                checked += 1
        self.assertGreater(checked, 20)

    def test_attributes_and_limits(self):
        lyr = layer(self.gpkg, "Airspace")
        names = lyr.fields().names()
        for col in ("uuid", "designator", "type", "upper_limit_text", "lower_limit_text"):
            self.assertIn(col, names)
        pts = layer(self.gpkg, "DesignatedPoint_location")
        f = next(pts.getFeatures())
        p = f.geometry().asPoint()
        self.assertTrue(12 < p.x() < 19 and 48 < p.y() < 51.5, p.asWkt())

    def test_route_segments_have_lines(self):
        lyr = layer(self.gpkg, "RouteSegment")
        with_geom = sum(1 for f in lyr.getFeatures() if f.hasGeometry())
        self.assertGreater(with_geom, 0)

    def test_issues_table_exists(self):
        lyr = layer(self.gpkg, ISSUES_TABLE)
        self.assertGreaterEqual(lyr.featureCount(), 0)


@unittest.skipUnless(os.path.isfile(EAD_SDD) and os.path.isfile(BORDERS), "sample data not available")
class AdditiveImportTests(unittest.TestCase):
    def setUp(self):
        self.gpkg = os.path.join(tempfile.mkdtemp(), "additive.gpkg")

    def incomplete(self):
        return sum(1 for f in layer(self.gpkg, "Airspace").getFeatures() if f["geom_complete"] != 1)

    def test_adding_borders_later_completes_airspaces(self):
        run_import([EAD_SDD], self.gpkg, check_neighbours=False)
        self.assertEqual(self.incomplete(), 58)
        result = run_import([BORDERS], self.gpkg, check_neighbours=False, append=True)
        self.assertEqual(result.sources, [os.path.basename(EAD_SDD), os.path.basename(BORDERS)])
        self.assertEqual(self.incomplete(), 0)
        self.assertEqual(layer(self.gpkg, "GeoBorder_border").featureCount(), 25)
        info = store.inspect(self.gpkg)
        self.assertEqual(info.type_counts["GeoBorder"], 25)
        self.assertEqual(info.type_counts["Airspace"], 351)

    def test_reimporting_a_file_replaces_its_stored_copy(self):
        run_import([EAD_SDD, BORDERS], self.gpkg, check_neighbours=False)
        run_import([EAD_SDD], self.gpkg, check_neighbours=False, append=True)
        info = store.inspect(self.gpkg)
        self.assertEqual(info.type_counts["Airspace"], 351)  # not 702
        self.assertEqual(sorted(info.sources), sorted(os.path.basename(p) for p in (EAD_SDD, BORDERS)))

    def test_class_selection_keeps_references_working(self):
        run_import([EAD_SDD, BORDERS], self.gpkg, check_neighbours=False,
                   classes={"Airspace", "RouteSegment"})
        names = {n for n in store.inspect(self.gpkg).layer_names}
        self.assertIn("Airspace", names)
        self.assertIn("RouteSegment", names)
        self.assertNotIn("DesignatedPoint_location", names)
        self.assertNotIn("GeoBorder_border", names)
        self.assertEqual(self.incomplete(), 0)  # GeoBorder not imported as layer, still used
        lines = layer(self.gpkg, "RouteSegment")
        self.assertEqual(sum(1 for f in lines.getFeatures() if f.hasGeometry()), 71)  # 1 point missing in data
        # a later update with no new files can add classes back; obsolete layers disappear
        run_import([], self.gpkg, check_neighbours=False, append=True, classes={"DesignatedPoint"})
        names = set(store.inspect(self.gpkg).layer_names)
        self.assertIn("DesignatedPoint_location", names)
        self.assertNotIn("Airspace", names)
        self.assertEqual(store.inspect(self.gpkg).classes, {"DesignatedPoint"})

    def test_foreign_geopackage_is_not_touched(self):
        from osgeo import ogr
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(self.gpkg)
        ds.CreateLayer("mine", None, ogr.wkbNone)
        ds = None
        with self.assertRaises(ValueError):
            run_import([BORDERS], self.gpkg)
        self.assertEqual(store.inspect(self.gpkg).layer_names, ["mine"])


@unittest.skipUnless(os.path.isfile(EAD_SDD), "sample data not available")
class EadSddImportTests(unittest.TestCase):
    """EAD SDD export: default-namespace message, '#uuid.' hrefs, EAD extensions."""

    def test_import_with_borders(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "bd.gpkg")
        result = run_import([EAD_SDD, BORDERS], gpkg)
        self.assertEqual(result.versions, ["5.1", "5.1.1"])
        lyr = layer(gpkg, "Airspace")
        self.assertEqual(lyr.featureCount(), 351)
        incomplete = [f["designator"] for f in lyr.getFeatures() if f["geom_complete"] != 1]
        self.assertEqual(incomplete, [])
        # the only error is a route point whose DesignatedPoint is not in the file
        errors = [(i.layer, i.message) for i in result.issues if i.severity == "error"]
        self.assertEqual(errors, [("RouteSegment", "unresolved point urn:uuid:55cafafc-47cf-4acb-bccb-950eed783b61")])
        # misaligned neighbours are warnings with a location
        misaligned = [i for i in result.issues if i.kind == "misaligned_boundary"]
        self.assertGreater(len(misaligned), 0)
        self.assertTrue(all(i.geometry is not None for i in misaligned))

    def test_without_borders_gaps_are_not_double_reported(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "bd_only.gpkg")
        result = run_import([EAD_SDD], gpkg, check_neighbours=False)
        kinds = {i.kind for i in result.issues}
        self.assertIn("unresolved_reference", kinds)
        self.assertNotIn("junction_gap", kinds)  # the jump over the missing border is not a 2nd issue
        lyr = layer(gpkg, "Airspace")
        self.assertEqual(sum(1 for f in lyr.getFeatures() if f["geom_complete"] != 1), 58)


if __name__ == "__main__":
    unittest.main()
