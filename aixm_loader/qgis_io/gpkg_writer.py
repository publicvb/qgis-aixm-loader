"""Write LayerData collections into a GeoPackage with GDAL/OGR."""
import os

from osgeo import ogr, osr

ogr.UseExceptions()

# tables of the GeoPackage that a re-import must keep: QGIS layer styles (remembered user styles)
KEEP_TABLES = {"layer_styles"}

_GEOM_TYPES = {
    "point": ogr.wkbPoint,
    "line": ogr.wkbMultiLineString,
    "polygon": ogr.wkbMultiPolygon,
    None: ogr.wkbNone,
}


def _wgs84():
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _field_type(values):
    kinds = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            kinds.add("int")
        elif isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("real")
        else:
            return ogr.OFTString
    if kinds == {"int"}:
        return ogr.OFTInteger64
    if kinds and kinds <= {"int", "real"}:
        return ogr.OFTReal
    return ogr.OFTString


def _columns(rows):
    """Ordered union of column names; case-insensitive duplicates renamed."""
    cols, seen = [], {}
    for attrs, _ in rows:
        for k in attrs:
            if k in seen:
                continue
            name = k
            lower = {c.lower() for c in seen.values()}
            n = 2
            while name.lower() in lower or name.lower() in ("fid", "geom"):
                name = "%s_%d" % (k, n)
                n += 1
            seen[k] = name
            cols.append(k)
    return cols, seen


class GpkgWriter:
    """Writes layers into a new or existing GeoPackage.

    An existing file is updated in place (layers are replaced one by one, and
    ``drop_other_layers`` removes the ones no longer produced) rather than
    deleted, so layers QGIS still has open do not block the import.
    """

    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            self.ds = ogr.Open(path, 1)
        else:
            self.ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
        self.srs = _wgs84()
        self.written = []  # (name, kind, count) of map/attribute layers
        self.created = set()  # every layer / table written in this session

    def write_layer(self, layer_data):
        rows = layer_data.rows
        if not rows:
            return
        kind = layer_data.kind
        geom_type = _GEOM_TYPES[kind]
        if kind == "point" and any(g is not None and g.isMultipart() for _, g in rows):
            geom_type = ogr.wkbMultiPoint
        opts = ["FID=fid", "OVERWRITE=YES"]
        if kind is not None:
            opts += ["GEOMETRY_NAME=geom", "SPATIAL_INDEX=YES"]
        lyr = self.ds.CreateLayer(layer_data.name, self.srs if kind else None, geom_type, opts)
        cols, names = _columns(rows)
        types = {}
        for k in cols:
            types[k] = _field_type(a.get(k) for a, _ in rows)
            lyr.CreateField(ogr.FieldDefn(names[k], types[k]))
        defn = lyr.GetLayerDefn()
        idx = {k: defn.GetFieldIndex(names[k]) for k in cols}
        lyr.StartTransaction()
        for attrs, geom in rows:
            feat = ogr.Feature(defn)
            for k, v in attrs.items():
                if v is None:
                    continue
                if types[k] == ogr.OFTString and not isinstance(v, str):
                    v = str(v)
                feat.SetField(idx[k], v)
            if geom is not None and not geom.isEmpty():
                ogr_geom = ogr.CreateGeometryFromWkb(bytes(geom.asWkb()))
                if kind == "point" and geom_type == ogr.wkbMultiPoint:
                    ogr_geom = ogr.ForceToMultiPoint(ogr_geom)
                feat.SetGeometry(ogr_geom)
            lyr.CreateFeature(feat)
        lyr.CommitTransaction()
        self.written.append((layer_data.name, kind, len(rows)))
        self.created.add(layer_data.name)

    def write_table(self, name, columns, rows, binary=()):
        """Plain attribute table from a list of tuples; columns in ``binary`` hold bytes."""
        lyr = self.ds.CreateLayer(name, None, ogr.wkbNone, ["FID=fid", "OVERWRITE=YES"])
        for c in columns:
            lyr.CreateField(ogr.FieldDefn(c, ogr.OFTBinary if c in binary else ogr.OFTString))
        defn = lyr.GetLayerDefn()
        is_bin = [c in binary for c in columns]
        lyr.StartTransaction()
        for r in rows:
            feat = ogr.Feature(defn)
            for i, v in enumerate(r):
                if v is None:
                    continue
                if is_bin[i]:
                    feat.SetFieldBinaryFromHexString(i, v.hex())
                else:
                    feat.SetField(i, str(v))
            lyr.CreateFeature(feat)
        lyr.CommitTransaction()
        self.created.add(name)

    def drop_other_layers(self):
        """Remove layers of an earlier import that this import did not write."""
        names = [self.ds.GetLayerByIndex(i).GetName() for i in range(self.ds.GetLayerCount())]
        for name in names:
            if name not in self.created and name not in KEEP_TABLES:
                for i in range(self.ds.GetLayerCount()):
                    if self.ds.GetLayerByIndex(i).GetName() == name:
                        self.ds.DeleteLayer(i)
                        break

    def close(self):
        self.ds = None
