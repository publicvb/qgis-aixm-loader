"""Tests for the QGIS-independent core (run with any Python 3.9+)."""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aixm_loader.core import geodesy  # noqa: E402
from aixm_loader.core.dataset import AixmDataset, iter_geometries  # noqa: E402
from aixm_loader.core.flatten import flatten  # noqa: E402
from aixm_loader.core.model import Measure, Ref  # noqa: E402
from aixm_loader.core.resolve import follow_curve  # noqa: E402
from aixm_loader.core.xmlutil import parse_href  # noqa: E402

SAMPLES = os.path.join(ROOT, "assets", "sample_xml_files")

HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<message:AIXMBasicMessage xmlns:message="http://www.aixm.aero/schema/5.2/message"
  xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:aixm="http://www.aixm.aero/schema/5.2"
  xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  gml:id="m1">
"""


def ts(ftype, body, interp="BASELINE", seq=1, begin="2020-01-01T00:00:00Z", end=None):
    end_xml = ("<gml:endPosition>%s</gml:endPosition>" % end) if end else \
        '<gml:endPosition indeterminatePosition="unknown"/>'
    return """<aixm:timeSlice><aixm:{t}TimeSlice gml:id="ts{seq}{interp}">
      <gml:validTime><gml:TimePeriod gml:id="tp{seq}{interp}"><gml:beginPosition>{b}</gml:beginPosition>{e}
      </gml:TimePeriod></gml:validTime>
      <aixm:interpretation>{interp}</aixm:interpretation>
      <aixm:sequenceNumber>{seq}</aixm:sequenceNumber><aixm:correctionNumber>0</aixm:correctionNumber>
      {body}</aixm:{t}TimeSlice></aixm:timeSlice>""".format(t=ftype, seq=seq, interp=interp, b=begin,
                                                             e=end_xml, body=body)


def feature(ftype, uuid, *slices):
    return """<message:hasMember><aixm:{t} gml:id="f{u}">
      <gml:identifier codeSpace="urn:uuid:">{u}</gml:identifier>{s}</aixm:{t}></message:hasMember>
    """.format(t=ftype, u=uuid, s="".join(slices))


U_DP = "11111111-1111-1111-1111-111111111111"
# arc around (15, 50), 10 NM, bearing 0 -> 90; the geodesic legs meet its ends
_N = geodesy.destination(15.0, 50.0, 0, 18520)
_E = geodesy.destination(15.0, 50.0, 90, 18520)
LEG1 = "50.0 14.0 %.9f %.9f" % (_N[1], _N[0])
LEG2 = "%.9f %.9f 50.0 14.0" % (_E[1], _E[0])
U_AS = "22222222-2222-2222-2222-222222222222"

SAMPLE = HEADER + feature(
    "DesignatedPoint", U_DP,
    ts("DesignatedPoint", """<aixm:designator>ABCDE</aixm:designator>
       <aixm:location><aixm:Point gml:id="p1" srsName="urn:ogc:def:crs:EPSG::4326">
       <gml:pos>50.0 15.0</gml:pos></aixm:Point></aixm:location>"""),
    ts("DesignatedPoint", "<aixm:designator>ABCDF</aixm:designator>", interp="TEMPDELTA", seq=1,
       begin="2024-01-01T00:00:00Z", end="2024-02-01T00:00:00Z"),
) + feature(
    "Airspace", U_AS,
    ts("Airspace", """<aixm:designator>TEST</aixm:designator><aixm:geometryComponent>
      <aixm:AirspaceGeometryComponent gml:id="c1"><aixm:operation>BASE</aixm:operation>
      <aixm:operationSequence>1</aixm:operationSequence><aixm:theAirspaceVolume>
      <aixm:AirspaceVolume gml:id="v1"><aixm:upperLimit uom="FL">95</aixm:upperLimit>
      <aixm:horizontalProjection><aixm:Surface gml:id="s1" srsName="urn:ogc:def:crs:EPSG::4326">
      <gml:patches><gml:PolygonPatch><gml:exterior><gml:Ring><gml:curveMember><gml:Curve gml:id="c2">
      <gml:segments>
        <gml:GeodesicString><gml:posList>{leg1}</gml:posList></gml:GeodesicString>
        <gml:ArcByCenterPoint numArc="1"><gml:pointProperty xlink:href="urn:uuid:{dp}"/>
          <gml:radius uom="NM">10</gml:radius>
          <gml:startAngle uom="deg">0</gml:startAngle><gml:endAngle uom="deg">90</gml:endAngle>
        </gml:ArcByCenterPoint>
        <gml:GeodesicString><gml:posList>{leg2}</gml:posList></gml:GeodesicString>
      </gml:segments></gml:Curve></gml:curveMember></gml:Ring></gml:exterior></gml:PolygonPatch>
      </gml:patches></aixm:Surface></aixm:horizontalProjection></aixm:AirspaceVolume>
      </aixm:theAirspaceVolume></aixm:AirspaceGeometryComponent></aixm:geometryComponent>
      <aixm:annotation xsi:nil="true"/>""".format(dp=U_DP, leg1=LEG1, leg2=LEG2)),
) + "</message:AIXMBasicMessage>"


def load_sample():
    fd, path = tempfile.mkstemp(suffix=".xml")
    with io.open(fd, "w", encoding="utf-8") as fh:
        fh.write(SAMPLE)
    ds = AixmDataset()
    ds.load([path])
    os.remove(path)
    return ds


class SyntheticTests(unittest.TestCase):
    def test_parse_and_version(self):
        ds = load_sample()
        self.assertEqual(ds.versions, {"5.2"})
        self.assertEqual(set(ds.features), {U_DP, U_AS})
        self.assertEqual(len(ds.features[U_DP].timeslices), 2)

    def test_axis_order_lat_lon_swapped(self):
        ds = load_sample()
        ds.snapshot()
        self.assertEqual(ds.feature_point(ds.states[U_DP]), (15.0, 50.0))

    def test_tempdelta_applies_only_inside_validity(self):
        ds = load_sample()
        inside = datetime(2024, 1, 15, tzinfo=timezone.utc)
        outside = datetime(2024, 3, 1, tzinfo=timezone.utc)
        self.assertEqual(ds.snapshot(inside)[U_DP].props["designator"], "ABCDF")
        self.assertEqual(ds.snapshot(outside)[U_DP].props["designator"], "ABCDE")
        self.assertNotIn(U_DP, ds.snapshot(datetime(2019, 1, 1, tzinfo=timezone.utc)))

    def test_arc_centre_via_xlinked_point(self):
        ds = load_sample()
        ds.snapshot(datetime(2025, 1, 1, tzinfo=timezone.utc))
        geoms = list(iter_geometries(ds.states[U_AS].props))
        self.assertEqual(len(geoms), 1)
        res = ds.resolver.resolve(geoms[0][1])
        self.assertEqual(res.issues, [])
        ring = res.polygons[0][0]
        self.assertEqual(ring[0], ring[-1])
        # arc from bearing 0 to 90 at 10 NM around (15, 50): passes north-east
        north = geodesy.destination(15.0, 50.0, 0, 18520)
        east = geodesy.destination(15.0, 50.0, 90, 18520)
        self.assertTrue(any(geodesy.distance(p, north) < 1 for p in ring))
        self.assertTrue(any(geodesy.distance(p, east) < 1 for p in ring))
        # spherical distance() vs ellipsoidal arc radius differ by up to ~0.3 %
        on_arc = [p for p in ring if abs(geodesy.distance((15.0, 50.0), p) - 18520) < 100]
        self.assertGreater(len(on_arc), 40)
        self.assertTrue(all(0 <= geodesy.bearing((15.0, 50.0), p) <= 90.01 or
                            geodesy.bearing((15.0, 50.0), p) > 359.99 for p in on_arc))

    def test_flatten(self):
        row = flatten({"designator": "X", "upperLimit": Measure("95", "FL"),
                       "airport": Ref("uuid", "abc")}, label_lookup=lambda r: "LKPR")
        self.assertEqual(row, {"designator": "X", "upperLimit": 95, "upperLimit_uom": "FL",
                               "airport_uuid": "abc", "airport_label": "LKPR"})


class GeodesyTests(unittest.TestCase):
    def test_destination_known_distance(self):
        p = geodesy.destination(0.0, 0.0, 90.0, 111319.49)
        self.assertAlmostEqual(p[0], 1.0, places=4)
        self.assertAlmostEqual(p[1], 0.0, places=6)

    def test_href_forms(self):
        self.assertEqual(parse_href("urn:uuid:ABC-1"), ("uuid", "abc-1"))
        self.assertEqual(parse_href("#uuid.abc"), ("uuid", "abc"))
        self.assertEqual(parse_href("#id12"), ("gmlid", "id12"))
        self.assertEqual(parse_href("#xpointer(//*[@gml:id='id9'])"), ("gmlid", "id9"))

    def test_follow_curve_both_directions(self):
        border = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
        self.assertEqual(follow_curve(border, (0.5, 0.1), (2.5, -0.1)),
                         [(0.5, 0.0), (1.0, 0.0), (2.0, 0.0), (2.5, 0.0)])
        self.assertEqual(follow_curve(border, (2.5, 0.0), (0.5, 0.0)),
                         [(2.5, 0.0), (2.0, 0.0), (1.0, 0.0), (0.5, 0.0)])

    def test_follow_closed_curve_takes_short_way(self):
        square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
        way = follow_curve(square, (0.0, 0.5), (0.5, 0.0))
        self.assertEqual(way, [(0.0, 0.5), (0.0, 0.0), (0.5, 0.0)])


@unittest.skipUnless(os.path.isdir(SAMPLES), "sample data not available")
class SampleDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = AixmDataset()
        cls.ds.load([os.path.join(SAMPLES, "LKAA_FULL_20230713.xml"),
                     os.path.join(SAMPLES, "Geoborder_260806_dataset.xml")])
        cls.ds.snapshot()

    def test_versions_and_counts(self):
        self.assertEqual(self.ds.versions, {"5.1", "5.1.1"})
        types = self.ds.by_type()
        self.assertEqual(len(types["Airspace"]), 350)
        self.assertEqual(len(types["GeoBorder"]), 25)

    def test_border_following_airspaces_resolve(self):
        unresolved = []
        for st in self.ds.states.values():
            if st.type != "Airspace":
                continue
            for _, g, _ in iter_geometries(st.props):
                unresolved += self.ds.resolver.resolve(g).issues
        self.assertEqual(unresolved, [])


if __name__ == "__main__":
    unittest.main()
