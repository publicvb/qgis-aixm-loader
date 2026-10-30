"""Smoke tests of dialog, task and project loading (offscreen Qt)."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication, QgsProject  # noqa: E402

_app = QgsApplication.instance()
if _app is None:
    _app = QgsApplication([], True)
    _app.initQgis()

from qgis.PyQt.QtCore import QSettings  # noqa: E402

from aixm_loader.dialog import (  # noqa: E402
    CHECKED, UNCHECKED, USER_ROLE, ImportDialog, default_target, forget_imports, previous_imports,
    remember_import,
)
from aixm_loader.qgis_io.importer import run_import  # noqa: E402
from aixm_loader.qgis_io.project_loader import (  # noqa: E402
    add_to_project, delete_imports, issues_layer, project_imports, remove_from_project,
)
from aixm_loader.qgis_io.task import AixmImportTask  # noqa: E402
from tests import aixm_samples as S  # noqa: E402

SAMPLES = os.path.join(ROOT, "assets", "sample_xml_files")
LKAA = os.path.join(SAMPLES, "LKAA_FULL_20230713.xml")
BORDERS = os.path.join(SAMPLES, "Geoborder_260806_dataset.xml")


def items(dlg):
    return {dlg.classes.item(i).data(USER_ROLE): dlg.classes.item(i) for i in range(dlg.classes.count())}


@unittest.skipUnless(os.path.isfile(LKAA), "sample data not available")
class DialogTests(unittest.TestCase):
    def test_new_import_lists_classes_with_counts(self):
        dlg = ImportDialog(sync_scan=True)
        dlg.output.setFilePath(os.path.join(tempfile.mkdtemp(), "new.gpkg"))
        dlg.inputs.setFilePath('"%s" "%s"' % (LKAA, BORDERS))
        self.assertEqual(dlg.paths(), [LKAA, BORDERS])
        found = items(dlg)
        self.assertIn("Airspace  (350)", found["Airspace"].text())
        self.assertIn("GeoBorder", found)
        self.assertIsNone(dlg.selected_classes())  # all checked = everything
        self.assertTrue(dlg.ok_button.isEnabled())
        found["VerticalStructure"].setCheckState(UNCHECKED)
        self.assertNotIn("VerticalStructure", dlg.selected_classes())
        dlg._check_all(False)
        self.assertFalse(dlg.ok_button.isEnabled())  # nothing selected
        self.assertFalse(dlg.append())
        self.assertFalse(dlg.add_mode.isVisible())

    def test_existing_import_offers_add_mode_and_keeps_selection(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "existing.gpkg")
        run_import([LKAA], gpkg, check_neighbours=False, classes={"Airspace", "DesignatedPoint"})
        dlg = ImportDialog(sync_scan=True)
        dlg.output.setFilePath(gpkg)
        self.assertTrue(dlg.info.appendable)
        self.assertTrue(dlg.append())
        self.assertIn("LKAA_FULL_20230713.xml", dlg.existing.text())
        self.assertTrue(dlg.ok_button.isEnabled())  # no new files: update the existing import
        found = items(dlg)
        self.assertEqual(found["Airspace"].checkState(), CHECKED)
        self.assertEqual(found["Navaid"].checkState(), UNCHECKED)  # stored, not selected before
        dlg.inputs.setFilePath(BORDERS)
        found = items(dlg)
        self.assertEqual(found["GeoBorder"].checkState(), CHECKED)  # new class: selected
        self.assertIn("stored", found["Airspace"].text())
        self.assertEqual(dlg.selected_classes(), {"Airspace", "DesignatedPoint", "GeoBorder"})
        dlg.inputs.setFilePath(LKAA)  # re-importing a stored file replaces it: no double count
        self.assertTrue(items(dlg)["Airspace"].text().startswith("Airspace  (350)"))
        dlg.inputs.setFilePath(BORDERS)
        dlg.replace_mode.setChecked(True)
        self.assertFalse(dlg.append())
        self.assertNotIn("Navaid", items(dlg))  # replace: only classes of the new files

    def test_new_files_are_added_to_the_default_import(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "current.gpkg")
        run_import([LKAA], gpkg, check_neighbours=False)
        dlg = ImportDialog(sync_scan=True, default_output=gpkg)
        dlg.inputs.setFilePath(BORDERS)
        self.assertEqual(dlg.gpkg_path(), gpkg)  # not Geoborder_260806_dataset.gpkg
        self.assertTrue(dlg.append())
        self.assertIn("New files will be added to it", dlg.existing.text())
        # choosing another file explicitly still creates a separate import
        other = os.path.join(tempfile.mkdtemp(), "separate.gpkg")
        dlg.output.setFilePath(other)
        self.assertFalse(dlg.append())

    def test_foreign_file_blocks_import(self):
        from osgeo import ogr
        gpkg = os.path.join(tempfile.mkdtemp(), "foreign.gpkg")
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
        ds.CreateLayer("mine", None, ogr.wkbNone)
        ds = None
        dlg = ImportDialog(sync_scan=True)
        dlg.output.setFilePath(gpkg)
        dlg.inputs.setFilePath(BORDERS)
        self.assertFalse(dlg.ok_button.isEnabled())
        self.assertIn("not created by AIXM Loader", dlg.message.text())


@unittest.skipUnless(os.path.isfile(LKAA), "sample data not available")
class TaskAndProjectTests(unittest.TestCase):
    def tearDown(self):
        QgsProject.instance().clear()

    def test_task_run_and_add_to_project(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "out.gpkg")
        done = []
        task = AixmImportTask([LKAA, BORDERS], gpkg, None, True, lambda t, ok: done.append(ok))
        self.assertTrue(task.run(), task.error)
        task.finished(True)
        self.assertEqual(done, [True])
        layers = add_to_project(gpkg, task.result.layers, "AIXM test")
        self.assertGreater(len(layers), 10)
        group = QgsProject.instance().layerTreeRoot().findGroup("AIXM test")
        self.assertIsNotNone(group)
        self.assertIsNotNone(group.findGroup("Issues"))
        airspace = QgsProject.instance().mapLayersByName("Airspace")[0]
        self.assertEqual(airspace.renderer().type(), "categorizedSymbol")

    def test_import_loaded_in_project_is_the_default_target(self):
        first = os.path.join(tempfile.mkdtemp(), "first.gpkg")
        second = os.path.join(tempfile.mkdtemp(), "second.gpkg")
        add_to_project(first, run_import([BORDERS], first, check_neighbours=False).layers, "first")
        add_to_project(second, run_import([BORDERS], second, check_neighbours=False).layers, "second")
        imports = project_imports()
        self.assertEqual([os.path.basename(p) for p in imports], ["second.gpkg", "first.gpkg"])  # top-most first
        self.assertEqual(default_target(imports), imports[0])

    def test_reimport_swaps_layers_in_the_same_group(self):
        gpkg = os.path.join(tempfile.mkdtemp(), "swap.gpkg")
        res = run_import([LKAA], gpkg, check_neighbours=False)
        add_to_project(gpkg, res.layers, "My AIXM")
        project = QgsProject.instance()
        self.assertEqual(len(project.mapLayersByName("aixm_issues_points")), 1)  # unresolved borders
        self.assertEqual(remove_from_project(gpkg), "My AIXM")
        self.assertEqual(len(project.mapLayers()), 0)
        self.assertIsNone(project.layerTreeRoot().findGroup("My AIXM"))
        res = run_import([BORDERS], gpkg, check_neighbours=False, append=True)
        add_to_project(gpkg, res.layers, "My AIXM")
        self.assertIsNotNone(project.layerTreeRoot().findGroup("My AIXM"))
        self.assertEqual(len(project.mapLayersByName("GeoBorder_border")), 1)
        self.assertEqual(project.mapLayersByName("aixm_issues_points"), [])  # borders resolved now
        airspace = project.mapLayersByName("Airspace")[0]
        self.assertEqual(sum(1 for f in airspace.getFeatures() if f["geom_complete"] != 1), 0)


class DeletePreviousImportsTests(unittest.TestCase):
    """Synthetic data: runs without the sample files."""

    KEYS = ("aixm_loader/imports", "aixm_loader/last_gpkg")

    def setUp(self):
        s = QSettings()
        self._saved = {k: s.value(k) for k in self.KEYS}
        for k in self.KEYS:
            s.remove(k)
        self.dir = tempfile.mkdtemp()
        self.xml = S.write(S.message(S.polygon_airspace("cccccccc-0000-0000-0000-00000000000a", "A",
                                                        [(15.0, 50.0), (15.1, 50.0), (15.1, 50.1),
                                                         (15.0, 50.0)])))

    def tearDown(self):
        QgsProject.instance().clear()
        S.cleanup(self.xml)
        s = QSettings()
        for k, v in self._saved.items():
            if v is None:
                s.remove(k)
            else:
                s.setValue(k, v)

    def gpkg(self, name):
        path = os.path.join(self.dir, name)
        run_import([self.xml], path, check_neighbours=False)
        return path

    def test_only_own_existing_imports_are_offered(self):
        first, second = self.gpkg("first.gpkg"), self.gpkg("second.gpkg")
        from osgeo import ogr
        foreign = os.path.join(self.dir, "foreign.gpkg")
        ogr.GetDriverByName("GPKG").CreateDataSource(foreign).CreateLayer("mine", None, ogr.wkbNone)
        for p in (first, foreign, os.path.join(self.dir, "gone.gpkg"), first):
            remember_import(p)
        loaded = self.gpkg("loaded.gpkg")  # imported before imports were recorded, open in the project
        add_to_project(loaded, run_import([self.xml], loaded, check_neighbours=False).layers, "loaded")
        QSettings().setValue("aixm_loader/last_gpkg", second)
        self.assertEqual(previous_imports(project_imports()), [first, second, loaded])

    def test_delete_removes_layers_and_files(self):
        kept, gone = self.gpkg("kept.gpkg"), self.gpkg("gone.gpkg")
        remember_import(kept)
        remember_import(gone)
        add_to_project(gone, run_import([self.xml], gone, check_neighbours=False).layers, "gone")
        self.assertTrue(QgsProject.instance().mapLayers())
        deleted, failed = delete_imports([gone, self.xml], trash=False)
        self.assertEqual(deleted, [gone])
        self.assertEqual([p for p, _ in failed], [self.xml])  # not a GeoPackage of ours: untouched
        self.assertTrue(os.path.isfile(self.xml))
        self.assertFalse(os.path.exists(gone))
        self.assertEqual(QgsProject.instance().mapLayers(), {})
        self.assertIsNone(QgsProject.instance().layerTreeRoot().findGroup("gone"))
        forget_imports(deleted)
        self.assertEqual(previous_imports(), [kept])


class _Bar:
    def __init__(self):
        self.infos = []

    def pushInfo(self, title, text):
        self.infos.append(text)


class _Iface:
    def __init__(self):
        self.bar = _Bar()
        self.shown = []

    def messageBar(self):
        return self.bar

    def showAttributeTable(self, layer):
        self.shown.append(layer.source())


class ShowIssuesTests(unittest.TestCase):
    """Synthetic data: runs without the sample files."""

    def tearDown(self):
        QgsProject.instance().clear()

    def test_button_of_an_earlier_import_opens_the_current_table(self):
        # "Show issues" of a message from before a later Add into the same GeoPackage
        # used to hit the deleted layer (RuntimeError: wrapped C/C++ object ... has been deleted)
        from aixm_loader.plugin import AixmLoaderPlugin
        open_ring = [(15.0, 50.0), (15.1, 50.0), (15.1, 50.1), (15.0, 50.1)]  # not closed: an issue
        first = S.write(S.message(S.polygon_airspace("dddddddd-0000-0000-0000-00000000000a", "A", open_ring)))
        second = S.write(S.message(S.polygon_airspace("dddddddd-0000-0000-0000-00000000000b", "B", open_ring)))
        gpkg = os.path.join(tempfile.mkdtemp(), "issues.gpkg")
        try:
            add_to_project(gpkg, run_import([first], gpkg, check_neighbours=False).layers, "imp")
            old = issues_layer(gpkg)
            self.assertIsNotNone(old)
            remove_from_project(gpkg)  # what the plugin does before adding the next file
            add_to_project(gpkg, run_import([second], gpkg, check_neighbours=False, append=True).layers, "imp")
        finally:
            S.cleanup(first)
            S.cleanup(second)
        from qgis.PyQt import sip
        self.assertTrue(sip.isdeleted(old))  # the layer the old button used to hold
        iface = _Iface()
        plugin = AixmLoaderPlugin(iface)
        plugin._show_issues(gpkg)  # the earlier message's button
        self.assertEqual(iface.shown, [issues_layer(gpkg).source()])
        QgsProject.instance().clear()
        plugin._show_issues(gpkg)
        self.assertEqual(len(iface.bar.infos), 1)  # gone from the project: a note, no error


if __name__ == "__main__":
    unittest.main()
