"""Background QgsTask running the import off the GUI thread."""
import traceback

from qgis.core import Qgis, QgsMessageLog, QgsTask

from ..compat import qenum
from .importer import run_import

LOG_TAG = "AIXM Loader"


def log(msg, level="Info"):
    QgsMessageLog.logMessage(msg, LOG_TAG, qenum(Qgis, "MessageLevel", level))


class AixmImportTask(QgsTask):
    def __init__(self, paths, gpkg_path, effective, include_tables, on_done, tolerance_m=50.0,
                 check_neighbours=True, classes=None, append=False):
        super().__init__("Importing AIXM into %s" % gpkg_path)
        self.paths = paths
        self.gpkg_path = gpkg_path
        self.effective = effective
        self.include_tables = include_tables
        self.tolerance_m = tolerance_m
        self.check_neighbours = check_neighbours
        self.classes = classes
        self.append = append
        self.on_done = on_done
        self.result = None
        self.error = None

    def run(self):
        try:
            # the task itself provides setProgress() / isCanceled()
            self.result = run_import(self.paths, self.gpkg_path, self.effective,
                                     self.include_tables, feedback=self, log=log,
                                     tolerance_m=self.tolerance_m,
                                     check_neighbours=self.check_neighbours,
                                     classes=self.classes, append=self.append)
        except Exception:  # reported in finished() on the GUI thread
            self.error = traceback.format_exc()
            return False
        return not self.result.cancelled

    def finished(self, ok):
        self.on_done(self, ok)
