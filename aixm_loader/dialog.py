"""Import dialog (built in code: no .ui file, so it works on Qt5 and Qt6 alike)."""
import os
from datetime import timezone

from qgis.core import QgsApplication, QgsTask
from qgis.gui import QgsFileWidget
from qgis.PyQt.QtCore import QDateTime, QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from .compat import qenum
from .core.parser import AixmParser
from .qgis_io import store
from .qgis_io.project_loader import delete_imports, project_imports

SETTINGS = "aixm_loader/"


def default_target(project_imports):
    """Import that new files are added to by default.

    The AIXM Loader GeoPackage highest in the project's layer tree, else the
    last one imported into (if it still exists and can be added to).
    """
    if project_imports:
        return project_imports[0]
    last = QSettings().value(SETTINGS + "last_gpkg", "")
    if last and store.inspect(last).appendable:
        return last
    return None


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def _stored_imports():
    value = QSettings().value(SETTINGS + "imports", [])
    if isinstance(value, str):  # QSettings returns a single-item list as a string
        value = [value]
    return [p for p in (value or []) if p]


def remember_import(path):
    """Record a GeoPackage this plugin writes to (offered later by "Delete previous imports")."""
    paths = _stored_imports()
    if _key(path) not in {_key(p) for p in paths}:
        QSettings().setValue(SETTINGS + "imports", paths + [path])


def forget_imports(gone):
    gone = {_key(p) for p in gone}
    QSettings().setValue(SETTINGS + "imports", [p for p in _stored_imports() if _key(p) not in gone])


def previous_imports(extra=()):
    """Existing AIXM Loader GeoPackages made by this plugin: recorded ones, the last used one
    and ``extra`` (e.g. those loaded in the project, also covering imports made before
    imports were recorded)."""
    out, seen = [], set()
    for p in _stored_imports() + [QSettings().value(SETTINGS + "last_gpkg", "")] + list(extra):
        if p and _key(p) not in seen and os.path.isfile(p) and store.inspect(p).is_aixm:
            seen.add(_key(p))
            out.append(p)
    return out


CHECKED = qenum(Qt, "CheckState", "Checked")
UNCHECKED = qenum(Qt, "CheckState", "Unchecked")
USER_ROLE = qenum(Qt, "ItemDataRole", "UserRole")


class ScanTask(QgsTask):
    """Counts feature classes per file in the background."""

    def __init__(self, paths, on_done):
        super().__init__("Scanning AIXM files", qenum(QgsTask, "Flag", "CanCancel"))
        self.paths = paths
        self.on_done = on_done
        self.counts = {}  # path -> {type: n}
        self.errors = {}  # path -> message

    def run(self):
        for i, p in enumerate(self.paths):
            try:
                self.counts[p] = AixmParser().scan(
                    p, lambda f, i=i: self.setProgress(100.0 * (i + f) / len(self.paths)), self.isCanceled)
            except Exception as e:  # malformed XML etc.: shown in the dialog
                self.errors[p] = str(e)
        return not self.isCanceled()

    def finished(self, ok):
        self.on_done(self, ok)


class DeleteImportsDialog(QDialog):
    """Checklist of previous imports to move to the recycle bin."""

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Delete previous imports")
        self.resize(700, 360)
        text = QLabel("These GeoPackages were created by AIXM Loader. The checked ones are moved to the "
                      "recycle bin and their layers removed from the project.")
        text.setWordWrap(True)
        self.files = QListWidget()
        for p in paths:
            size = os.path.getsize(p) / 1048576.0
            item = QListWidgetItem("%s  (%.1f MB)  –  %s" % (os.path.basename(p), size, os.path.dirname(p)))
            item.setData(USER_ROLE, p)
            item.setToolTip(p)
            item.setFlags(item.flags() | qenum(Qt, "ItemFlag", "ItemIsUserCheckable"))
            item.setCheckState(CHECKED)
            self.files.addItem(item)
        self.files.itemChanged.connect(self._validate)
        buttons = QDialogButtonBox(qenum(QDialogButtonBox, "StandardButton", "Cancel"))
        self.delete_button = buttons.addButton("Move to recycle bin",
                                               qenum(QDialogButtonBox, "ButtonRole", "AcceptRole"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addWidget(self.files, 1)
        lay.addWidget(buttons)

    def selected(self):
        items = [self.files.item(i) for i in range(self.files.count())]
        return [it.data(USER_ROLE) for it in items if it.checkState() == CHECKED]

    def _validate(self):
        self.delete_button.setEnabled(bool(self.selected()))


class ImportDialog(QDialog):
    def __init__(self, parent=None, sync_scan=False, default_output=None):
        """default_output: existing import to add to (see default_target()); new files
        are then added to it instead of creating a GeoPackage next to them."""
        super().__init__(parent)
        self.setWindowTitle("Import AIXM 5.x")
        self.resize(1000, 640)
        self._sync_scan = sync_scan
        self._scan_cache = {}  # path -> {type: n}
        self._scan_errors = {}
        self._scan_task = None
        self._user_checks = {}  # feature type -> bool, choices made in the list
        self._last_output = None
        self.info = store.inspect("")
        s = QSettings()

        self.inputs = QgsFileWidget()
        self.inputs.setStorageMode(qenum(QgsFileWidget, "StorageMode", "GetMultipleFiles"))
        self.inputs.setFilter("AIXM / GML (*.xml *.aixm *.gml);;All files (*)")
        self.inputs.setDefaultRoot(s.value(SETTINGS + "last_dir", ""))
        self.inputs.fileChanged.connect(self._inputs_changed)

        self.output = QgsFileWidget()
        self.output.setStorageMode(qenum(QgsFileWidget, "StorageMode", "SaveFile"))
        self.output.setFilter("GeoPackage (*.gpkg)")
        self.output.setConfirmOverwrite(False)  # replacing vs adding is chosen below
        self.output.fileChanged.connect(self._output_changed)

        files_hint = QLabel("Files imported together (or added later to the same GeoPackage) resolve "
                            "each other's xlinks, e.g. a national dataset and its GeoBorder file.")
        files_hint.setWordWrap(True)

        self.existing = QLabel()
        self.existing.setWordWrap(True)
        # radio buttons / check boxes do not wrap: keep their texts short, explain in labels
        self.add_mode = QRadioButton("Add to the existing import")
        self.add_mode.setToolTip("The new files are merged with the stored features and all layers are rebuilt.")
        self.replace_mode = QRadioButton("Replace the existing import")
        self.add_mode.setChecked(True)
        self.add_mode.toggled.connect(self._refresh)
        mode_box = QVBoxLayout()
        mode_box.setContentsMargins(0, 0, 0, 0)
        for w in (self.existing, self.add_mode, self.replace_mode):
            mode_box.addWidget(w)

        self.cleanup_button = QPushButton("Delete previous imports…")
        self.cleanup_button.setToolTip("Move GeoPackages created by earlier imports to the recycle bin.")
        self.cleanup_button.clicked.connect(self._delete_previous)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output, 1)
        output_row.addWidget(self.cleanup_button)

        form = QFormLayout()
        form.addRow("AIXM files", self.inputs)
        form.addRow("", files_hint)
        form.addRow("Output GeoPackage", output_row)
        form.addRow("", mode_box)

        # feature classes
        self.classes = QListWidget()
        self.classes.setMinimumHeight(160)
        self.classes.itemChanged.connect(self._item_changed)
        self.scan_status = QLabel()
        all_btn, none_btn = QPushButton("Select all"), QPushButton("Select none")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn.clicked.connect(lambda: self._check_all(False))
        class_box = QGroupBox("Feature classes to import")
        cl = QVBoxLayout(class_box)
        cl.addWidget(self.classes, 1)
        row = QHBoxLayout()
        row.addWidget(self.scan_status, 1)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        cl.addLayout(row)
        note = QLabel("Unselected classes get no layers but are kept in the GeoPackage and still used to "
                      "build geometry (e.g. the points of route segments).")
        note.setWordWrap(True)
        cl.addWidget(note)

        self.latest = QRadioButton("Latest state of each feature")
        self.at_date = QRadioButton("Valid at (UTC)")
        self.date = QDateTimeEdit(QDateTime.currentDateTimeUtc())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.latest.setChecked(s.value(SETTINGS + "latest", True, type=bool))
        self.at_date.setChecked(not self.latest.isChecked())
        self.date.setEnabled(self.at_date.isChecked())
        self.at_date.toggled.connect(self.date.setEnabled)
        time_box = QGroupBox("Temporality (timeslices)")
        tl = QVBoxLayout(time_box)
        tl.addWidget(self.latest)
        row = QHBoxLayout()
        row.addWidget(self.at_date)
        row.addWidget(self.date, 1)
        tl.addLayout(row)

        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.0, 10000.0)
        self.tolerance.setDecimals(1)
        self.tolerance.setSuffix(" m")
        self.tolerance.setValue(s.value(SETTINGS + "tolerance", 50.0, type=float))
        self.tolerance.setToolTip(
            "Segments that do not meet, vertices off the border they follow, and vertices of one "
            "airspace lying this close to another airspace's boundary without being on it are "
            "reported as alignment issues. 0 disables the checks.")
        self.neighbours = QCheckBox("Check alignment between neighbouring airspaces")
        self.neighbours.setChecked(s.value(SETTINGS + "neighbours", True, type=bool))
        check_box = QGroupBox("Geometry checks")
        ckl = QFormLayout(check_box)
        ckl.addRow("Alignment tolerance", self.tolerance)
        ckl.addRow(self.neighbours)

        self.tables = QCheckBox("Write features without geometry as tables")
        self.tables.setToolTip("Feature classes without any geometry (e.g. Runway, Unit) become attribute tables.")
        self.tables.setChecked(s.value(SETTINGS + "tables", True, type=bool))
        self.load = QCheckBox("Add layers to the project")
        self.load.setChecked(s.value(SETTINGS + "load", True, type=bool))

        buttons = QDialogButtonBox(
            qenum(QDialogButtonBox, "StandardButton", "Ok") | qenum(QDialogButtonBox, "StandardButton", "Cancel"))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(qenum(QDialogButtonBox, "StandardButton", "Ok"))
        self.ok_button.setText("Import")

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #b00020")
        self.message.setTextInteractionFlags(qenum(Qt, "TextInteractionFlag", "TextSelectableByMouse"))

        # two columns keep the dialog low enough for laptop screens; the scroll area guarantees
        # widgets never overlap when the screen is still too small (Qt would squeeze them)
        left = QVBoxLayout()
        left.addLayout(form)
        left.addWidget(time_box)
        left.addWidget(check_box)
        left.addWidget(self.tables)
        left.addWidget(self.load)
        left.addStretch(1)
        body = QWidget()
        columns = QHBoxLayout(body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.addLayout(left, 3)
        columns.addWidget(class_box, 2)
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(qenum(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
        scroll.setFrameShape(qenum(QFrame, "Shape", "NoFrame"))

        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.message)
        lay.addWidget(buttons)
        if default_output:
            self.output.setFilePath(default_output)  # triggers _output_changed
        self._output_changed()

    # -- public ---------------------------------------------------------
    def paths(self):
        return [p for p in QgsFileWidget.splitFilePaths(self.inputs.filePath()) if p]

    def gpkg_path(self):
        p = self.output.filePath().strip()
        if p and not p.lower().endswith(".gpkg"):
            p += ".gpkg"
        return p

    def append(self):
        """True when the files are added to an existing import."""
        return self.info.appendable and self.add_mode.isChecked()

    def selected_classes(self):
        """Checked feature classes, or None when all are checked (also includes classes added later)."""
        items = [self.classes.item(i) for i in range(self.classes.count())]
        checked = {it.data(USER_ROLE) for it in items if it.checkState() == CHECKED}
        if items and len(checked) == len(items):
            return None
        return checked

    def effective(self):
        """Aware UTC datetime, or None for 'latest'."""
        if self.latest.isChecked():
            return None
        dt = self.date.dateTime().toUTC().toPyDateTime()
        return dt.replace(tzinfo=timezone.utc)

    # -- output / mode ----------------------------------------------------
    def _output_changed(self):
        path = self.gpkg_path()
        self.info = store.inspect(path) if path else store.inspect("")
        show = self.info.exists
        for w in (self.existing, self.add_mode, self.replace_mode):
            w.setVisible(show)
        if self.info.appendable:
            n = sum(self.info.type_counts.values())
            self.existing.setText("Existing import of %d feature(s) from: %s. New files will be added to "
                                  "it and all layers rebuilt. Choose another output file to create a "
                                  "separate import." % (n, ", ".join(self.info.sources) or "?"))
            self.add_mode.setEnabled(True)
            self.replace_mode.setEnabled(True)
            if path != self._last_output:  # a newly chosen existing import: add by default
                self.add_mode.setChecked(True)
        elif self.info.is_aixm:
            self.existing.setText("This GeoPackage holds an import made by an older version, which "
                                  "cannot be added to; it will be replaced.")
            self.add_mode.setEnabled(False)
            self.replace_mode.setChecked(True)
        elif self.info.exists:
            self.existing.setText("This file exists and was not created by AIXM Loader.")
            self.add_mode.setEnabled(False)
            self.replace_mode.setEnabled(False)
        self._last_output = path
        self._refresh()

    # -- scanning ----------------------------------------------------------
    def _inputs_changed(self):
        paths = self.paths()
        if paths and not self.output.filePath():  # no existing import chosen: new one next to the input
            self.output.setFilePath(os.path.splitext(paths[0])[0] + ".gpkg")
        todo = [p for p in paths if os.path.isfile(p) and p not in self._scan_cache]
        if todo:
            self._start_scan(todo)
        self._refresh()

    def _start_scan(self, paths):
        if self._scan_task is not None:
            self._scan_task.cancel()
        task = ScanTask(paths, self._scan_done)
        self._scan_task = task
        if self._sync_scan:
            task.finished(task.run())
        else:
            QgsApplication.taskManager().addTask(task)
        self._refresh()

    def _scan_done(self, task, ok):
        if task is not self._scan_task:
            return  # superseded by a newer scan
        self._scan_task = None
        self._scan_cache.update(task.counts)
        self._scan_errors.update(task.errors)
        missing = [p for p in self.paths() if os.path.isfile(p) and p not in self._scan_cache
                   and p not in self._scan_errors]
        if missing and ok:
            self._start_scan(missing)
        self._refresh()

    # -- class list ----------------------------------------------------------
    def _class_counts(self):
        """{type: (stored count, new count)} for the list."""
        out = {}
        if self.append():
            # a re-imported file replaces its stored copy: count only the other stored files
            reimported = {os.path.basename(p) for p in self.paths()}
            for source, counts in self.info.source_type_counts.items():
                if source in reimported:
                    continue
                for t, n in counts.items():
                    out.setdefault(t, [0, 0])[0] += n
        for p in self.paths():
            for t, n in self._scan_cache.get(p, {}).items():
                out.setdefault(t, [0, 0])[1] += n
        return out

    def _default_check(self, t, stored, new):
        if t in self._user_checks:
            return self._user_checks[t]
        if new and not stored:
            return True
        prev = self.info.classes if self.append() else None
        return prev is None or t in prev

    def _rebuild_list(self):
        counts = self._class_counts()
        self.classes.blockSignals(True)
        self.classes.clear()
        for t in sorted(counts):
            stored, new = counts[t]
            if stored and new:
                text = "%s  (%d stored, %d in new files)" % (t, stored, new)
            elif stored:
                text = "%s  (%d stored)" % (t, stored)
            else:
                text = "%s  (%d)" % (t, new)
            item = QListWidgetItem(text)
            item.setData(USER_ROLE, t)
            item.setFlags(item.flags() | qenum(Qt, "ItemFlag", "ItemIsUserCheckable"))
            item.setCheckState(CHECKED if self._default_check(t, stored, new) else UNCHECKED)
            self.classes.addItem(item)
        self.classes.blockSignals(False)

    def _item_changed(self, item):
        self._user_checks[item.data(USER_ROLE)] = item.checkState() == CHECKED
        self._validate()

    def _check_all(self, state):
        for i in range(self.classes.count()):
            self.classes.item(i).setCheckState(CHECKED if state else UNCHECKED)

    # -- validation ------------------------------------------------------------
    def _refresh(self):
        self._rebuild_list()
        scanning = self._scan_task is not None
        if scanning:
            self.scan_status.setText("Scanning files for feature classes…")
        else:
            n = self.classes.count()
            self.scan_status.setText("%d class(es) found" % n if n else "")
        self._validate()

    def _validate(self):
        paths = self.paths()
        msg = ""
        missing = [p for p in paths if not os.path.isfile(p)]
        broken = [p for p in paths if p in self._scan_errors]
        if missing:
            msg = "File not found: %s" % missing[0]
        elif broken:
            msg = "Cannot read %s: %s" % (os.path.basename(broken[0]), self._scan_errors[broken[0]])
        elif self.info.exists and not self.info.is_aixm:
            msg = "The output file exists and was not created by AIXM Loader; choose another file."
        elif self.gpkg_path() and os.path.abspath(self.gpkg_path()) in [os.path.abspath(p) for p in paths]:
            msg = "The output must not be one of the input files."
        self.message.setText(msg)
        has_input = bool(paths) or self.append()  # adding nothing = rebuild with new options
        selected = self.selected_classes()
        ok = (not msg and has_input and bool(self.gpkg_path()) and self._scan_task is None
              and (selected is None or bool(selected)))
        self.ok_button.setEnabled(ok)

    def _accept(self):
        s = QSettings()
        if self.paths():
            s.setValue(SETTINGS + "last_dir", os.path.dirname(self.paths()[0]))
        s.setValue(SETTINGS + "latest", self.latest.isChecked())
        s.setValue(SETTINGS + "tables", self.tables.isChecked())
        s.setValue(SETTINGS + "load", self.load.isChecked())
        s.setValue(SETTINGS + "tolerance", self.tolerance.value())
        s.setValue(SETTINGS + "neighbours", self.neighbours.isChecked())
        s.setValue(SETTINGS + "last_gpkg", self.gpkg_path())
        remember_import(self.gpkg_path())
        self.accept()

    # -- deleting previous imports --------------------------------------------
    def _delete_previous(self):
        candidates = previous_imports(project_imports())
        if not candidates:
            QMessageBox.information(self, "Delete previous imports", "No previous imports were found.")
            return
        dlg = DeleteImportsDialog(candidates, self)
        if not dlg.exec():
            return
        deleted, failed = delete_imports(dlg.selected())
        forget_imports(deleted)
        out = self.gpkg_path()
        if out and _key(out) in {_key(p) for p in deleted}:
            self.output.setFilePath("")  # a new output is proposed next to the input files
            self._inputs_changed()
        self._output_changed()
        if failed:
            QMessageBox.warning(self, "Delete previous imports", "Not deleted:\n\n" + "\n".join(
                "%s: %s" % (p, reason) for p, reason in failed))

    def reject(self):
        if self._scan_task is not None:
            self._scan_task.cancel()
        super().reject()
