"""QGIS plugin entry: menu / toolbar action, dialog, background import."""
import os

from qgis.core import Qgis, QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMessageBox, QPushButton

from .compat import QAction, qenum
from .dialog import ImportDialog, default_target
from .qgis_io.importer import ISSUES_TABLE
from .qgis_io.project_loader import (
    add_to_project, issues_layer, project_imports, remove_from_project, restyle_project_layers,
)
from .qgis_io.styles import StyleMemory
from .qgis_io.task import AixmImportTask, log

MENU = "&AIXM Loader"
ISSUE_TEXT = {
    "unresolved_reference": "unresolved reference(s)",
    "geometry_error": "geometry error(s)",
    "junction_gap": "gap(s) between segments",
    "off_border": "vertex(es) off the followed border",
    "ring_not_closed": "unclosed ring(s)",
    "invalid_geometry": "self-intersection(s) repaired",
    "misaligned_boundary": "misaligned airspace boundary stretch(es)",
    "vertex_mismatch": "vertex mismatch(es) in compositions",
    "vertex_off_edge": "vertex(es) off a component edge",
    "parse_error": "parse error(s)",
}


class AixmLoaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.reset_action = None
        self.styles = None
        self.tasks = set()  # keep references: tasks are garbage-collected otherwise
        self._load_after = {}

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Import AIXM 5.x…", self.iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToVectorMenu(MENU, self.action)
        self.reset_action = QAction("Reset remembered styles…", self.iface.mainWindow())
        self.reset_action.triggered.connect(self.reset_styles)
        self.iface.addPluginToVectorMenu(MENU, self.reset_action)
        # remember how imported layers are styled, also for layers of saved projects
        self.styles = StyleMemory()
        self.styles.attach()

    def unload(self):
        if self.styles is not None:
            self.styles.detach()
            self.styles = None
        self.iface.removePluginVectorMenu(MENU, self.action)
        self.iface.removePluginVectorMenu(MENU, self.reset_action)
        self.iface.removeToolBarIcon(self.action)
        self.action = self.reset_action = None

    def reset_styles(self):
        answer = QMessageBox.question(
            self.iface.mainWindow(), "AIXM Loader",
            "Forget all remembered layer styles?\n\nThe saved styles are removed from your QGIS profile "
            "and from the AIXM GeoPackages in this project; their layers get the plugin's default "
            "styling again.")
        if answer != qenum(QMessageBox, "StandardButton", "Yes"):
            return
        gpkgs = project_imports()
        self.styles.reset(gpkgs)
        with self.styles.paused():
            restyle_project_layers(gpkgs)
        self.iface.messageBar().pushSuccess("AIXM Loader", "Remembered styles were reset.")

    def show_dialog(self):
        dlg = ImportDialog(self.iface.mainWindow(), default_output=default_target(project_imports()))
        if not dlg.exec():
            return
        # layers of this GeoPackage are rebuilt: keep pending style changes, then take them out
        self.styles.flush()
        group_name = remove_from_project(dlg.gpkg_path())
        task = AixmImportTask(dlg.paths(), dlg.gpkg_path(), dlg.effective(),
                              dlg.tables.isChecked(), self._task_done,
                              tolerance_m=dlg.tolerance.value(),
                              check_neighbours=dlg.neighbours.isChecked(),
                              classes=dlg.selected_classes(), append=dlg.append())
        self._load_after[id(task)] = (dlg.load.isChecked() or group_name is not None,
                                      dlg.tables.isChecked(), group_name)
        self.tasks.add(task)
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushInfo("AIXM Loader", "Import started – see the task manager for progress.")

    def _task_done(self, task, ok):
        self.tasks.discard(task)
        load, tables, group_name = self._load_after.pop(id(task), (True, True, None))
        bar = self.iface.messageBar()
        if task.error:
            log(task.error, "Critical")
            bar.pushCritical("AIXM Loader", "Import failed – details in the log panel (AIXM Loader).")
            return
        res = task.result
        if res is None or res.cancelled or not ok:
            bar.pushWarning("AIXM Loader", "Import cancelled.")
            return
        for w in res.warnings:
            log(w, "Warning")
        for i in res.issues:
            log("[%s] %s %s (%s): %s" % (i.kind, i.feature_type or "", i.label or "", i.uuid or "", i.message),
                "Info" if i.severity == "info" else "Warning")
        added = add_to_project(res.gpkg_path, res.layers, group_name, include_tables=tables,
                               styles=self.styles) if load else []
        msg = "Imported %d features into %d layers from %d file(s) (AIXM %s)." % (
            res.layer_feature_count, len(res.layers), len(res.sources), ", ".join(res.versions) or "?")
        summary = res.issue_summary()
        if not summary:
            bar.pushSuccess("AIXM Loader", msg + " No geometry or alignment issues.")
            return
        msg += " Issues: " + ", ".join("%d %s" % (n, ISSUE_TEXT.get(k, k)) for k, n in summary.items())
        has_table = any(l.name() == ISSUES_TABLE for l in added)
        self._push_issues(msg, res.gpkg_path if has_table else None,
                          any(i.severity == "error" for i in res.issues))

    def _push_issues(self, msg, gpkg_path, has_errors):
        """Message-bar warning with a button opening the issues table of gpkg_path."""
        bar = self.iface.messageBar()
        item = bar.createMessage("AIXM Loader", msg)
        if gpkg_path:
            button = QPushButton("Show issues")
            button.clicked.connect(lambda: self._show_issues(gpkg_path))
            item.layout().addWidget(button)
        level = "Critical" if has_errors else "Warning"
        bar.pushWidget(item, qenum(Qgis, "MessageLevel", level), 0)

    def _show_issues(self, gpkg_path):
        # looked up on click: a later import into the same GeoPackage replaces its layers,
        # so a layer kept from this import may already be deleted
        layer = issues_layer(gpkg_path)
        if layer is not None:
            self.iface.showAttributeTable(layer)
        else:
            self.iface.messageBar().pushInfo("AIXM Loader", "The issues table of this import is no longer "
                                                            "in the project.")
