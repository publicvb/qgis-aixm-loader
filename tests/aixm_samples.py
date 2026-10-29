"""Builders for small synthetic AIXM documents used by the tests."""
import io
import os
import tempfile

NS = ('xmlns:message="http://www.aixm.aero/schema/5.1/message" '
      'xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:aixm="http://www.aixm.aero/schema/5.1" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')


def message(*members):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<message:AIXMBasicMessage %s gml:id="m">%s'
            '</message:AIXMBasicMessage>' % (NS, "".join(
                "<message:hasMember>%s</message:hasMember>" % m for m in members)))


def timeslice(ftype, body, gml_id):
    return ("<aixm:timeSlice><aixm:{t}TimeSlice gml:id=\"{i}\"><gml:validTime><gml:TimePeriod gml:id=\"{i}p\">"
            "<gml:beginPosition>2020-01-01T00:00:00Z</gml:beginPosition>"
            "<gml:endPosition indeterminatePosition=\"unknown\"/></gml:TimePeriod></gml:validTime>"
            "<aixm:interpretation>BASELINE</aixm:interpretation><aixm:sequenceNumber>1</aixm:sequenceNumber>"
            "<aixm:correctionNumber>0</aixm:correctionNumber>{b}</aixm:{t}TimeSlice></aixm:timeSlice>"
            ).format(t=ftype, i=gml_id, b=body)


def feature(ftype, uuid, body):
    return ('<aixm:{t} gml:id="uuid.{u}"><gml:identifier codeSpace="urn:uuid:">{u}</gml:identifier>{ts}'
            '</aixm:{t}>').format(t=ftype, u=uuid, ts=timeslice(ftype, body, "ts" + uuid[:8]))


def poslist(lonlats):
    return " ".join("%.9f %.9f" % (lat, lon) for lon, lat in lonlats)


def surface(gml_id, segments):
    return ('<aixm:Surface gml:id="{i}" srsName="urn:ogc:def:crs:EPSG::4326"><gml:patches><gml:PolygonPatch>'
            '<gml:exterior><gml:Ring><gml:curveMember><gml:Curve gml:id="{i}c"><gml:segments>{s}'
            '</gml:segments></gml:Curve></gml:curveMember></gml:Ring></gml:exterior></gml:PolygonPatch>'
            '</gml:patches></aixm:Surface>').format(i=gml_id, s="".join(segments))


def geodesic(lonlats):
    return "<gml:GeodesicString><gml:posList>%s</gml:posList></gml:GeodesicString>" % poslist(lonlats)


def component(op, seq, volume):
    return ("<aixm:geometryComponent><aixm:AirspaceGeometryComponent gml:id=\"gc{s}{o}\">"
            "<aixm:operation>{o}</aixm:operation><aixm:operationSequence>{s}</aixm:operationSequence>"
            "<aixm:theAirspaceVolume><aixm:AirspaceVolume gml:id=\"av{s}{o}\">{v}</aixm:AirspaceVolume>"
            "</aixm:theAirspaceVolume></aixm:AirspaceGeometryComponent></aixm:geometryComponent>"
            ).format(o=op, s=seq, v=volume)


def airspace(uuid, designator, *components):
    return feature("Airspace", uuid, "<aixm:type>CTA</aixm:type><aixm:designator>%s</aixm:designator>%s"
                   % (designator, "".join(components)))


def polygon_airspace(uuid, designator, ring, gml_id=None):
    """Airspace with one BASE component; ring is a closed list of (lon, lat)."""
    return airspace(uuid, designator, component(
        "BASE", 1, "<aixm:horizontalProjection>%s</aixm:horizontalProjection>"
        % surface(gml_id or "s" + uuid[:8], [geodesic(ring)])))


def contributor(uuid):
    return ('<aixm:contributorAirspace><aixm:AirspaceVolumeDependency gml:id="d{u}">'
            '<aixm:dependency>FULL_GEOMETRY</aixm:dependency>'
            '<aixm:theAirspace xlink:href="urn:uuid:{u}"/></aixm:AirspaceVolumeDependency>'
            '</aixm:contributorAirspace>').format(u=uuid)


def write(text):
    fd, path = tempfile.mkstemp(suffix=".xml")
    with io.open(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def cleanup(path):
    try:
        os.remove(path)
    except OSError:
        pass
