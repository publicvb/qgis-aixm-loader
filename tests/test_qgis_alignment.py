"""Alignment checks on synthetic airspaces (QGIS Python)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication, QgsVectorLayer  # noqa: E402

_app = QgsApplication.instance()
if _app is None:
    _app = QgsApplication([], False)
    _app.initQgis()

from aixm_loader.qgis_io.importer import ISSUE_LINES, ISSUES_TABLE, run_import  # noqa: E402
from tests import aixm_samples as S  # noqa: E402

UA = "bbbbbbbb-0000-0000-0000-00000000000a"
UB = "bbbbbbbb-0000-0000-0000-00000000000b"
UC = "bbbbbbbb-0000-0000-0000-00000000000c"
DX20 = 20.0 / (111320.0 * 0.6428)  # 20 m in longitude at 50 deg N


def square(x0, x1, y0=50.0, y1=50.1, shift_top_left=0.0):
    return [(x0 + shift_top_left, y1) if p == (x0, y1) else p
            for p in [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]


def run(*members, **kw):
    path = S.write(S.message(*members))
    gpkg = os.path.join(tempfile.mkdtemp(), "out.gpkg")
    try:
        return run_import([path], gpkg, **kw), gpkg
    finally:
        S.cleanup(path)


def kinds(result):
    return sorted(i.kind for i in result.issues if i.severity != "info")


class NeighbourAlignmentTests(unittest.TestCase):
    def test_aligned_neighbours_have_no_issues(self):
        r, _ = run(S.polygon_airspace(UA, "A", square(15.0, 15.1)),
                   S.polygon_airspace(UB, "B", square(15.1, 15.2)))
        self.assertEqual(kinds(r), [])

    def test_shifted_shared_vertex_is_reported_once_with_location(self):
        r, gpkg = run(S.polygon_airspace(UA, "A", square(15.0, 15.1)),
                      S.polygon_airspace(UB, "B", square(15.1, 15.2, shift_top_left=DX20)))
        issues = [i for i in r.issues if i.kind == "misaligned_boundary"]
        self.assertEqual(len(issues), 1, [i.message for i in issues])
        i = issues[0]
        self.assertAlmostEqual(i.size_m, 20, delta=1)
        self.assertIn("A", i.message)
        self.assertIn("B", i.message)
        c = i.geometry.centroid().asPoint()
        self.assertAlmostEqual(c.x(), 15.1, places=3)
        self.assertAlmostEqual(c.y(), 50.1, places=3)
        lines = QgsVectorLayer("%s|layername=%s" % (gpkg, ISSUE_LINES), "l", "ogr")
        self.assertEqual(lines.featureCount(), 1)
        airspaces = QgsVectorLayer("%s|layername=Airspace" % gpkg, "a", "ogr")
        self.assertEqual(sorted(f["issue_count"] for f in airspaces.getFeatures()), [1, 1])

    def test_below_tolerance_or_disabled_is_silent(self):
        members = (S.polygon_airspace(UA, "A", square(15.0, 15.1)),
                   S.polygon_airspace(UB, "B", square(15.1, 15.2, shift_top_left=DX20)))
        self.assertEqual(kinds(run(*members, tolerance_m=10)[0]), [])
        self.assertEqual(kinds(run(*members, check_neighbours=False)[0]), [])

    def test_tangent_boundaries_are_not_reported(self):
        # B's edge runs 30 m from A's edge for its whole length but never meets it: no shared vertex,
        # nothing is "meant" to coincide, so only vertices within tolerance count - here the corners
        # are 30 m apart diagonally, which is a genuine near-miss and is reported; a 200 m offset is not.
        far = 200.0 / (111320.0 * 0.6428)
        r, _ = run(S.polygon_airspace(UA, "A", square(15.0, 15.1)),
                   S.polygon_airspace(UB, "B", square(15.1 + far, 15.2)))
        self.assertEqual(kinds(r), [])


class CompositionAlignmentTests(unittest.TestCase):
    def test_union_of_misaligned_contributors_is_reported_on_the_union(self):
        a = S.polygon_airspace(UA, "A", square(15.0, 15.1))
        b = S.polygon_airspace(UB, "B", square(15.1, 15.2, shift_top_left=DX20))
        c = S.airspace(UC, "C", S.component("BASE", 1, S.contributor(UA)),
                       S.component("UNION", 2, S.contributor(UB)))
        r, gpkg = run(a, b, c)
        on_c = [i for i in r.issues if i.uuid == UC]
        self.assertTrue(on_c and all(i.kind in ("vertex_mismatch", "vertex_off_edge") for i in on_c),
                        [(i.kind, i.message) for i in on_c])
        self.assertTrue(any("UNION contributor B" in i.message for i in on_c))
        # A and B are siblings in C's union: not reported a second time as neighbours
        self.assertEqual([i for i in r.issues if i.kind == "misaligned_boundary"], [])
        table = QgsVectorLayer("%s|layername=%s" % (gpkg, ISSUES_TABLE), "t", "ogr")
        self.assertEqual(table.featureCount(), len(r.issues))

    def test_aligned_union_is_clean(self):
        a = S.polygon_airspace(UA, "A", square(15.0, 15.1))
        b = S.polygon_airspace(UB, "B", square(15.1, 15.2))
        c = S.airspace(UC, "C", S.component("BASE", 1, S.contributor(UA)),
                       S.component("UNION", 2, S.contributor(UB)))
        r, gpkg = run(a, b, c)
        self.assertEqual(kinds(r), [])
        lyr = QgsVectorLayer("%s|layername=Airspace" % gpkg, "a", "ogr")
        union = next(f for f in lyr.getFeatures() if f["uuid"] == UC).geometry()
        self.assertEqual(len(union.asMultiPolygon()), 1)  # no sliver, one polygon
        self.assertEqual(len(union.asMultiPolygon()[0]), 1)  # no hole


if __name__ == "__main__":
    unittest.main()
