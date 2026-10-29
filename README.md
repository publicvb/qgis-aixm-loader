# AIXM Loader – QGIS plugin

Imports AIXM 5.1 / 5.1.1 / 5.2 XML into a GeoPackage and adds styled layers to QGIS
(3.34 LTR and 4.x). See `project.md` for the project goal.

## Layout

```
aixm_loader/            the plugin (this folder is what QGIS loads)
  core/                 pure Python, no QGIS import – parser, geometry, timeslices
    parser.py           streaming parser; features found structurally (gml:identifier + timeSlice)
    geometry.py         GML/AIXM geometry -> unresolved segment descriptions (Seg/Geom), lat/lon -> lon/lat
    resolve.py          segments -> coordinates: geodesic densify, arcs/circles, xlinked points,
                        border following (GeoBorder), arc end snapping
    geodesy.py          WGS84 Vincenty direct, great-circle helpers
    timeslice.py        BASELINE/PERMDELTA/TEMPDELTA -> state valid at a date
    dataset.py          several files loaded together so xlinks resolve across files
    flatten.py          property trees -> attribute columns (+ _uom, _uuid, _label, JSON for nested)
  qgis_io/
    builder.py          rows + QgsGeometry per layer; Airspace composition, RouteSegment lines
    gpkg_writer.py      GDAL/OGR GeoPackage writer
    importer.py         end-to-end run_import() (thread-safe, used by the task and tests)
    task.py             QgsTask wrapper
    project_loader.py   adds layers to the project with styles / labels
  dialog.py, plugin.py  UI
tests/                  unittest suites (core runs on plain Python; qgis_* need QGIS Python)
scripts/                run_tests.ps1, install_dev.ps1
assets/sample_xml_files real sample data (LKAA national set, LKTB AMDB, GeoBorder)
```

## Output

* `Airspace` – geometry composed from all `geometryComponent`s (BASE/UNION/SUBTR/INTERS,
  contributor airspaces, arcs, circles, GeoBorder following, corridor centreline+width), with
  vertical limits (`upper_limit_text`, ...), `geom_complete` and `geom_issues`.
* `RouteSegment` – `curveExtent`, else geodesic line between the start/end points.
* Every other geometry property becomes a layer `<FeatureType>_<property path>`
  (e.g. `VerticalStructure_part_horizontalProjection_location`) with the feature's attributes
  plus those of the owning object (`part_*`) and of the geometry (`geom_elevation`, ...).
* Feature types without geometry → attribute tables; `aixm_import` (metadata).

## Additive imports and class selection

Every imported feature's XML is stored compressed in `aixm_source_features`, with the import
settings in `aixm_import`. By default the dialog targets the existing import: the AIXM Loader
GeoPackage highest in the project's layer tree, else the last one imported into. New files are
then *added* to it; a new GeoPackage next to the input file is only proposed when no previous
import exists or the output field is cleared. With an existing import as output the dialog offers:

* **Add** – the new files are merged with the stored features and *all* layers are rebuilt,
  so e.g. a GeoBorder file imported afterwards completes the airspaces following that border.
  A file whose name is already stored replaces its earlier copy. Adding with no files
  re-applies changed options (classes, date, tolerance) to the stored data.
* **Replace** – start over with just the new files.

The dialog scans the input files and lists their feature classes with counts. Unselected
classes get no layers but stay stored and are still used to resolve xlinks (border following,
route segment end points, contributor airspaces). Layers of the GeoPackage that are open in
the project are removed before the import and re-added afterwards in the same group.
A GeoPackage not created by AIXM Loader is never modified.

## Remembered styles

When you restyle a layer of an import (symbology, labels, opacity / scale visibility, diagrams),
the plugin remembers it per layer name (e.g. `Airspace`) in two places:

* the GeoPackage's `layer_styles` table (as default style) – travels with the file and survives
  re-imports into it;
* `<QGIS profile>/aixm_loader/styles/<layer>.qml` – applies to every later import.

On import the GeoPackage's style wins, then the profile's, then the built-in styling. Values new
in a later import (e.g. an airspace type) are added to a remembered categorized style.
Vector → AIXM Loader → *Reset remembered styles…* forgets both copies.

## Accepted encodings

Anything schema-valid: any message wrapper (BasicMessage, WFS collections, ...) and namespace
prefixes; `urn:uuid:`, `#uuid.`, `#gml-id` and xpointer xlinks (geometries, curves and points
referenced by gml:id are inlined); srsName as URN / `EPSG:` / OGC http URI / CRS84 /
`epsg.xml#` plus `axisLabels`; pos / posList (2D, 3D) / coordinates; LineStringSegment,
GeodesicString, Geodesic, ArcByCenterPoint, CircleByCenterPoint, Arc, ArcString, Circle
(angles in deg / rad / grad); Polygon / Surface / MultiSurface, LinearRing / Ring,
OrientableCurve, CompositeCurve. Rare spline/bulge segments are approximated by their control
points with a note. A bad geometry or unsupported (non-geographic) CRS never drops the feature;
it is reported.

## Issues and alignment checks

Everything that does not fit is reported in `aixm_issues` (table), `aixm_issues_points` and
`aixm_issues_lines` (map layers, added in an "Issues" group), the Airspace columns
`geom_complete` / `issue_count` / `geom_issues`, and a message-bar summary with a
"Show issues" button. With alignment tolerance T (dialog, default 50 m):

| kind | meaning |
|---|---|
| `unresolved_reference` (error) | xlinked point / curve / contributor airspace not in the dataset |
| `geometry_error` (error) | unparseable geometry, unsupported CRS, empty composition result |
| `junction_gap` | consecutive segments end/start more than T apart |
| `off_border` | vertex more than T from the GeoBorder it is followed along |
| `ring_not_closed` | ring end more than T from its start |
| `invalid_geometry` | self-intersection, repaired (location given) |
| `vertex_mismatch` / `vertex_off_edge` | in a UNION/SUBTR/INTERS: a component vertex within T of another component's vertex / edge but not on it |
| `misaligned_boundary` | neighbouring airspaces: published vertices within T of the other's vertices/edges but not on them (gap or overlap slivers); nearby vertices are grouped into one stretch |

Offsets under 2 m (coordinate rounding) are ignored. Only published vertices are compared, so
tangent or diverging boundaries are not reported. Arc vertices are generated at whole-degree
bearings so adjacent airspaces sharing an arc get identical vertices.

## Development

Run tests with the QGIS Python (the system's execution policy may block the `.ps1` scripts;
the commands inside them can be run directly):

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
& "C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat" -m unittest discover -s tests -t . -v
```

Install for development by linking `aixm_loader` into the QGIS profile
(`%APPDATA%\QGIS\QGIS4\profiles\default\python\plugins\aixm_loader`), see `scripts/install_dev.ps1`.
