"""What an AIXM Loader GeoPackage contains: source features and import metadata.

Every imported feature's XML is kept (compressed) in SOURCE_TABLE, so a
later import into the same GeoPackage can rebuild all layers from the stored
features plus new files - e.g. adding a GeoBorder file afterwards repairs the
airspaces that follow that border.
"""
import json
import os

from osgeo import ogr

ogr.UseExceptions()

FORMAT = "aixm_loader/1"
META_TABLE = "aixm_import"
SOURCE_TABLE = "aixm_source_features"
SOURCE_COLUMNS = ("source_file", "uuid", "feature_type", "xml")


class StoreInfo:
    """Summary of an existing GeoPackage, for the dialog and for appending."""

    def __init__(self, path):
        self.path = path
        self.exists = os.path.isfile(path)
        self.is_aixm = False  # written by this plugin
        self.has_store = False  # can be appended to
        self.meta = {}
        self.sources = []
        self.classes = None  # selected classes of the last import (None = all)
        self.type_counts = {}  # feature type -> stored count
        self.source_type_counts = {}  # source file -> {feature type: stored count}
        self.layer_names = []
        self.error = None

    @property
    def appendable(self):
        return self.is_aixm and self.has_store


def _open(path):
    return ogr.Open(path, 0)  # raises (UseExceptions) when not a readable vector file


def inspect(path):
    info = StoreInfo(path)
    if not info.exists:
        return info
    try:
        ds = _open(path)
    except Exception as e:  # not a GeoPackage / unreadable
        info.error = str(e)
        return info
    if ds is None:
        info.error = "cannot be opened"
        return info
    info.layer_names = [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    meta = ds.GetLayerByName(META_TABLE)
    if meta is not None:
        info.is_aixm = True
        for f in meta:
            info.meta[f.GetField("key")] = f.GetField("value")
        info.sources = _json_list(info.meta.get("source_files")) or []
        classes = _json_list(info.meta.get("selected_classes"))
        info.classes = set(classes) if classes is not None else None
    src = ds.GetLayerByName(SOURCE_TABLE)
    if src is not None:
        info.has_store = True
        sql = ("SELECT source_file, feature_type, COUNT(*) AS n FROM %s GROUP BY source_file, feature_type"
               % SOURCE_TABLE)
        res = ds.ExecuteSQL(sql)
        for f in res:
            source, ftype, n = f.GetField(0), f.GetField(1), f.GetField(2)
            info.source_type_counts.setdefault(source, {})[ftype] = n
            info.type_counts[ftype] = info.type_counts.get(ftype, 0) + n
        ds.ReleaseResultSet(res)
    ds = None
    return info


def read_records(path, exclude_sources=()):
    """[(source, uuid, type, xml)] stored in an AIXM Loader GeoPackage."""
    ds = _open(path)
    lyr = ds.GetLayerByName(SOURCE_TABLE)
    out = []
    if lyr is not None:
        idx = [lyr.GetLayerDefn().GetFieldIndex(c) for c in SOURCE_COLUMNS]
        for f in lyr:
            source = f.GetField(idx[0])
            if source in exclude_sources:
                continue
            out.append((source, f.GetField(idx[1]), f.GetField(idx[2]), bytes(f.GetFieldAsBinary(idx[3]))))
    ds = None
    return out


def _json_list(value):
    if not value:
        return None
    try:
        v = json.loads(value)
    except ValueError:
        return [s.strip() for s in value.split(";") if s.strip()]  # pre-JSON metadata
    return v if isinstance(v, list) else None
