"""Schema-valid AIXM encoding variants must all parse to the same result (plain Python)."""
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aixm_loader.core import geodesy  # noqa: E402
from aixm_loader.core.dataset import AixmDataset, iter_geometries  # noqa: E402
from aixm_loader.core.geometry import axis_order  # noqa: E402
from tests import aixm_samples as S  # noqa: E402

U1 = "aaaaaaaa-0000-0000-0000-000000000001"
U2 = "aaaaaaaa-0000-0000-0000-000000000002"
U3 = "aaaaaaaa-0000-0000-0000-000000000003"
U4 = "aaaaaaaa-0000-0000-0000-000000000004"


def load(text):
    path = S.write(text)
    try:
        ds = AixmDataset()
        ds.load([path])
        ds.snapshot()
        return ds
    finally:
        S.cleanup(path)


def resolved(ds, uuid):
    out = []
    for _, g, _ in iter_geometries(ds.states[uuid].props):
        out.append(ds.resolver.resolve(g))
    return out


def point_body(pos_xml):
    return "<aixm:designator>P</aixm:designator><aixm:location>%s</aixm:location>" % pos_xml


class WrapperAndPrefixTests(unittest.TestCase):
    def test_wfs_collection_with_other_prefixes(self):
        text = """<?xml version="1.0"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:g="http://www.opengis.net/gml/3.2"
  xmlns:a="http://www.aixm.aero/schema/5.2" xmlns:xl="http://www.w3.org/1999/xlink">
 <wfs:member><a:DesignatedPoint g:id="uuid.%s">
   <g:identifier codeSpace="urn:uuid:">%s</g:identifier>
   <a:timeSlice><a:DesignatedPointTimeSlice g:id="t1">
     <g:validTime><g:TimePeriod g:id="tp"><g:beginPosition>2020-01-01T00:00:00Z</g:beginPosition>
       <g:endPosition indeterminatePosition="unknown"/></g:TimePeriod></g:validTime>
     <a:interpretation>BASELINE</a:interpretation>
     <a:designator>WFSPT</a:designator>
     <a:location><a:Point g:id="p"><g:pos>50 15</g:pos></a:Point></a:location>
   </a:DesignatedPointTimeSlice></a:timeSlice></a:DesignatedPoint></wfs:member>
</wfs:FeatureCollection>""" % (U1, U1)
        ds = load(text)
        self.assertEqual(ds.versions, {"5.2"})
        self.assertEqual(ds.feature_point(ds.states[U1]), (15.0, 50.0))

    def test_feature_without_identifier_uses_gml_id(self):
        body = point_body('<aixm:Point gml:id="p"><gml:pos>50 15</gml:pos></aixm:Point>')
        text = S.message(S.feature("DesignatedPoint", U1, body).replace(
            '<gml:identifier codeSpace="urn:uuid:">%s</gml:identifier>' % U1, ""))
        ds = load(text)
        self.assertIn(U1, ds.states)
        self.assertTrue(any("gml:id" in m for m in ds.features[U1].issues))


class CrsTests(unittest.TestCase):
    def test_axis_order_variants(self):
        self.assertEqual(axis_order("urn:ogc:def:crs:EPSG::4326"), "latlon")
        self.assertEqual(axis_order("urn:ogc:def:crs:EPSG:6.6:4326"), "latlon")
        self.assertEqual(axis_order("http://www.opengis.net/def/crs/EPSG/0/4326"), "latlon")
        self.assertEqual(axis_order("EPSG:4258"), "latlon")
        self.assertEqual(axis_order("urn:ogc:def:crs:OGC:1.3:CRS84"), "lonlat")
        self.assertEqual(axis_order("http://www.opengis.net/gml/srs/epsg.xml#4326"), "lonlat")
        self.assertEqual(axis_order("urn:ogc:def:crs:EPSG::4326", "Long Lat"), "lonlat")
        self.assertIsNone(axis_order("urn:ogc:def:crs:EPSG::3857"))

    def test_points_in_all_crs_forms_agree(self):
        variants = [
            '<aixm:Point gml:id="p" srsName="http://www.opengis.net/def/crs/EPSG/0/4326"><gml:pos>50 15</gml:pos></aixm:Point>',
            '<aixm:Point gml:id="p" srsName="urn:ogc:def:crs:OGC:1.3:CRS84"><gml:pos>15 50</gml:pos></aixm:Point>',
            '<aixm:Point gml:id="p" srsName="EPSG:4326" axisLabels="Long Lat"><gml:pos>15 50</gml:pos></aixm:Point>',
            '<aixm:ElevatedPoint gml:id="p"><gml:pos srsDimension="3">50 15 300</gml:pos>'
            '<aixm:elevation uom="FT">300</aixm:elevation></aixm:ElevatedPoint>',
            '<aixm:Point gml:id="p"><gml:coordinates>50,15</gml:coordinates></aixm:Point>',
        ]
        for v in variants:
            ds = load(S.message(S.feature("DesignatedPoint", U1, point_body(v))))
            self.assertEqual(ds.feature_point(ds.states[U1]), (15.0, 50.0), v)

    def test_unsupported_crs_is_reported_not_fatal(self):
        body = point_body('<aixm:Point gml:id="p" srsName="urn:ogc:def:crs:EPSG::3857">'
                          '<gml:pos>1669792 6446276</gml:pos></aixm:Point>')
        ds = load(S.message(S.feature("DesignatedPoint", U1, body)))
        self.assertEqual(ds.states[U1].props["designator"], "P")  # feature kept
        res = resolved(ds, U1)[0]
        self.assertEqual([i.kind for i in res.issues], ["geometry_error"])
        self.assertIn("unsupported CRS", res.issues[0].message)

    def test_broken_poslist_keeps_feature(self):
        body = point_body('<aixm:Point gml:id="p"><gml:posList>50 15 49</gml:posList></aixm:Point>')
        ds = load(S.message(S.feature("DesignatedPoint", U1, body)))
        self.assertIn(U1, ds.states)
        self.assertEqual(resolved(ds, U1)[0].issues[0].kind, "geometry_error")


class LocalReferenceTests(unittest.TestCase):
    RING = [(15.0, 50.0), (15.1, 50.0), (15.1, 50.1), (15.0, 50.1), (15.0, 50.0)]

    def test_geometry_by_reference(self):
        a = S.polygon_airspace(U1, "A", self.RING, gml_id="shared")
        b = S.airspace(U2, "B", S.component(
            "BASE", 1, '<aixm:horizontalProjection xlink:href="#shared"/>'))
        ds = load(S.message(a, b))
        ra, rb = resolved(ds, U1)[0], resolved(ds, U2)[0]
        self.assertEqual(rb.issues, [])
        self.assertEqual(ra.polygons, rb.polygons)

    def test_curve_member_and_point_by_gml_id(self):
        dp = S.feature("DesignatedPoint", U3, point_body(
            '<aixm:Point gml:id="centre"><gml:pos>50.05 15.05</gml:pos></aixm:Point>'))
        a = S.polygon_airspace(U1, "A", self.RING)  # defines curve "s<uuid>c"
        ring_ref = ('<aixm:Surface gml:id="s2"><gml:patches><gml:PolygonPatch><gml:exterior><gml:Ring>'
                    '<gml:curveMember xlink:href="#s%sc"/></gml:Ring></gml:exterior></gml:PolygonPatch>'
                    '</gml:patches></aixm:Surface>' % U1[:8])
        b = S.airspace(U2, "B", S.component("BASE", 1, "<aixm:horizontalProjection>%s</aixm:horizontalProjection>"
                                            % ring_ref))
        circle = ('<aixm:Surface gml:id="s4"><gml:patches><gml:PolygonPatch><gml:exterior><gml:Ring>'
                  '<gml:curveMember><gml:Curve gml:id="c4"><gml:segments><gml:CircleByCenterPoint>'
                  '<gml:pointProperty xlink:href="#centre"/><gml:radius uom="KM">2</gml:radius>'
                  '</gml:CircleByCenterPoint></gml:segments></gml:Curve></gml:curveMember></gml:Ring>'
                  '</gml:exterior></gml:PolygonPatch></gml:patches></aixm:Surface>')
        c = S.airspace(U4, "C", S.component("BASE", 1, "<aixm:horizontalProjection>%s</aixm:horizontalProjection>"
                                            % circle))
        ds = load(S.message(dp, a, b, c))
        rb = resolved(ds, U2)[0]
        self.assertEqual(rb.issues, [])
        self.assertEqual(rb.polygons, resolved(ds, U1)[0].polygons)
        rc = resolved(ds, U4)[0]
        self.assertEqual(rc.issues, [])
        d = [geodesy.distance((15.05, 50.05), p) for p in rc.polygons[0][0]]
        self.assertLess(max(abs(x - 2000) for x in d), 10)


class SegmentTests(unittest.TestCase):
    def _arc_airspace(self, seg_xml, start, end):
        segs = [S.geodesic([(15.0, 50.0), start]), seg_xml, S.geodesic([end, (15.0, 50.0)])]
        return S.airspace(U1, "A", S.component(
            "BASE", 1, "<aixm:horizontalProjection>%s</aixm:horizontalProjection>" % S.surface("s", segs)))

    def test_arc_angles_in_radians(self):
        n = geodesy.destination(15.0, 50.0, 0, 10000)
        e = geodesy.destination(15.0, 50.0, 90, 10000)
        arc = ('<gml:ArcByCenterPoint numArc="1"><gml:pos>50 15</gml:pos><gml:radius uom="M">10000</gml:radius>'
               '<gml:startAngle uom="rad">0</gml:startAngle><gml:endAngle uom="rad">%r</gml:endAngle>'
               '</gml:ArcByCenterPoint>' % (math.pi / 2))
        ds = load(S.message(self._arc_airspace(arc, n, e)))
        res = resolved(ds, U1)[0]
        self.assertEqual(res.issues, [])
        ring = res.polygons[0][0]
        on_arc = [p for p in ring if abs(geodesy.distance((15.0, 50.0), p) - 10000) < 60]
        self.assertGreater(len(on_arc), 80)

    def test_arcstring_with_two_arcs(self):
        pts = [geodesy.destination(15.0, 50.0, b, 10000) for b in (0, 45, 90, 135, 180)]
        arcs = '<gml:ArcString><gml:posList>%s</gml:posList></gml:ArcString>' % S.poslist(pts)
        ds = load(S.message(self._arc_airspace(arcs, pts[0], pts[-1])))
        res = resolved(ds, U1)[0]
        self.assertEqual(res.issues, [])
        ring = res.polygons[0][0]
        on_arc = [p for p in ring if abs(geodesy.distance((15.0, 50.0), p) - 10000) < 60]
        self.assertGreater(len(on_arc), 100)  # a real arc, not 5 straight-joined points

    def test_junction_gap_reported_with_location(self):
        ring = [S.geodesic([(15.0, 50.0), (15.1, 50.0)]),
                S.geodesic([(15.1, 50.002), (15.1, 50.1), (15.0, 50.0)])]  # ~220 m gap
        a = S.airspace(U1, "A", S.component(
            "BASE", 1, "<aixm:horizontalProjection>%s</aixm:horizontalProjection>" % S.surface("s", ring)))
        res = resolved(load(S.message(a)), U1)[0]
        gaps = [i for i in res.issues if i.kind == "junction_gap"]
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].size_m, 222, delta=5)
        self.assertAlmostEqual(gaps[0].location[1], 50.001, places=3)


if __name__ == "__main__":
    unittest.main()
