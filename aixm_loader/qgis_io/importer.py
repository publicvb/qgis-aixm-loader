"""End-to-end import: AIXM files -> GeoPackage. Safe to run in a worker thread."""
import json
import os
from collections import OrderedDict
from datetime import datetime, timezone

from qgis.core import Qgis

from ..core.dataset import AixmDataset
from . import store
from .builder import LayerBuilder, LayerData
from .gpkg_writer import GpkgWriter
from .store import META_TABLE, SOURCE_COLUMNS, SOURCE_TABLE

ISSUES_TABLE = "aixm_issues"
ISSUE_POINTS = "aixm_issues_points"
ISSUE_LINES = "aixm_issues_lines"
ISSUE_LAYERS = (ISSUE_POINTS, ISSUE_LINES)


class ImportResult:
    def __init__(self, gpkg_path):
        self.gpkg_path = gpkg_path
        self.layers = []  # (name, kind, count)
        self.sources = []  # all source files now in the GeoPackage
        self.feature_count = 0  # features valid at the snapshot (all classes)
        self.layer_feature_count = 0  # features of the selected classes
        self.issues = []
        self.warnings = []
        self.versions = []
        self.cancelled = False

    def issue_summary(self):
        """{kind: count} for warnings and errors (informational notes excluded)."""
        out = OrderedDict()
        for i in sorted(self.issues, key=lambda i: i.kind):
            if i.severity != "info":
                out[i.kind] = out.get(i.kind, 0) + 1
        return out


def run_import(paths, gpkg_path, effective=None, include_tables=True, feedback=None, log=None,
               tolerance_m=50.0, check_neighbours=True, classes=None, append=False):
    """Parse ``paths`` as one dataset, write all layers into ``gpkg_path``.

    effective: aware datetime of the snapshot, or None for the latest state.
    feedback: optional QgsFeedback (progress 0-100, cancellation).
    tolerance_m: alignment tolerance for gaps / misaligned boundaries.
    classes: feature types to write as layers (None = all). Features of other
        types are still stored and used to resolve references.
    append: add ``paths`` to the features already stored in ``gpkg_path``
        (a file with the same name as a stored one replaces it); all layers
        are rebuilt from the combined set.
    """
    log = log or (lambda msg: None)
    result = ImportResult(gpkg_path)
    cancelled = (lambda: feedback.isCanceled()) if feedback is not None else None

    info = store.inspect(gpkg_path)
    if info.exists and not info.is_aixm:
        raise ValueError("%s exists and was not created by AIXM Loader; choose another file" % gpkg_path)
    if append and not info.appendable:
        raise ValueError("%s holds no stored features to add to (created by an older version?); "
                         "import all files again instead" % gpkg_path)

    def parse_progress(fraction, path):
        if feedback is not None:
            feedback.setProgress(50.0 * fraction)

    ds = AixmDataset(tolerance_m=tolerance_m)
    if append:
        new_names = {os.path.basename(p) for p in paths}
        ds.load_records(store.read_records(gpkg_path, exclude_sources=new_names), cancelled)
        log("Loaded %d stored features from %s" % (len(ds.records), ", ".join(ds.sources) or "-"))
    ds.load(paths, parse_progress, cancelled, keep_xml=True)
    if cancelled and cancelled():
        result.cancelled = True
        return result
    ds.snapshot(effective)
    log("Parsed %d features (%d valid at snapshot) from %d file(s), AIXM %s"
        % (len(ds.features), len(ds.states), len(ds.sources), ", ".join(sorted(ds.versions)) or "?"))

    builder = LayerBuilder(ds, _SubFeedback(feedback, 50, 40) if feedback is not None else None,
                           tolerance_m=tolerance_m, check_neighbours=check_neighbours, classes=classes)
    layers = builder.build(include_tables)
    if cancelled and cancelled():
        result.cancelled = True
        return result

    issue_layers = _issue_layers(builder.issues, ds.warnings)
    os.makedirs(os.path.dirname(os.path.abspath(gpkg_path)), exist_ok=True)
    writer = GpkgWriter(gpkg_path)
    try:
        # the source store first: the GeoPackage stays rebuildable even if a later step fails
        writer.write_table(SOURCE_TABLE, list(SOURCE_COLUMNS), ds.records, binary=("xml",))
        all_layers = list(layers.values()) + issue_layers
        for i, lyr in enumerate(all_layers):
            if lyr.name == ISSUES_TABLE and not lyr.rows:  # keep an (empty) issues table
                writer.write_table(ISSUES_TABLE, list(_issue_row_columns()), [])
                writer.written.append((ISSUES_TABLE, None, 0))
                continue
            writer.write_layer(lyr)
            if feedback is not None:
                feedback.setProgress(90 + 10.0 * (i + 1) / max(1, len(all_layers)))
        writer.write_table(META_TABLE, ["key", "value"], [
            ("format", store.FORMAT),
            ("source_files", json.dumps(ds.sources, ensure_ascii=False)),
            ("selected_classes", json.dumps(sorted(classes)) if classes is not None else ""),
            ("aixm_versions", ", ".join(sorted(ds.versions))),
            ("effective_date", effective.isoformat() if effective else "latest"),
            ("alignment_tolerance_m", str(tolerance_m)),
            ("neighbour_check", "yes" if check_neighbours else "no"),
            ("imported_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("feature_count", str(len(ds.states))),
        ])
        writer.drop_other_layers()
    finally:
        writer.close()

    result.layers = writer.written
    result.sources = list(ds.sources)
    result.feature_count = len(ds.states)
    result.layer_feature_count = builder.output_feature_count
    result.issues = builder.issues
    result.warnings = ds.warnings
    result.versions = sorted(ds.versions)
    return result


def _issue_row(i):
    loc = i.geometry.pointOnSurface().asPoint() if i.geometry is not None and not i.geometry.isEmpty() else None
    return OrderedDict([
        ("severity", i.severity),
        ("kind", i.kind),
        ("feature_type", i.feature_type),
        ("uuid", i.uuid),
        ("label", i.label),
        ("layer", i.layer),
        ("message", i.message),
        ("size_m", round(i.size_m, 1) if i.size_m is not None else None),
        ("related_uuid", i.related_uuid),
        ("related_label", i.related_label),
        ("lon", loc.x() if loc else None),
        ("lat", loc.y() if loc else None),
    ])


def _issue_row_columns():
    return ("severity", "kind", "feature_type", "uuid", "label", "layer", "message", "size_m",
            "related_uuid", "related_label", "lon", "lat")


def _issue_layers(issues, warnings):
    """Issues table (all) plus point / line layers for located issues."""
    table = LayerData(ISSUES_TABLE, None)
    points = LayerData(ISSUE_POINTS, "point")
    lines = LayerData(ISSUE_LINES, "line")
    for i in issues:
        row = _issue_row(i)
        table.rows.append((row, None))
        g = i.geometry
        if g is None or g.isEmpty():
            continue
        if g.type() == Qgis.GeometryType.Point:
            points.rows.append((row, g))
        elif g.type() == Qgis.GeometryType.Line:
            g = type(g)(g)
            if not g.isMultipart():
                g.convertToMultiType()
            lines.rows.append((row, g))
    for w in warnings:
        table.rows.append((OrderedDict([("severity", "error"), ("kind", "parse_error"), ("message", w)]), None))
    return [table] + [lyr for lyr in (points, lines) if lyr.rows]


class _SubFeedback:
    """Maps 0-100 progress of a sub-step into [start, start + span] of a parent."""

    def __init__(self, parent, start, span):
        self.parent, self.start, self.span = parent, start, span

    def setProgress(self, p):
        self.parent.setProgress(self.start + self.span * p / 100.0)

    def isCanceled(self):
        return self.parent.isCanceled()
