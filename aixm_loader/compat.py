"""Small helpers to run unchanged on QGIS 3.34+ (Qt5) and QGIS 4 (Qt6)."""
try:
    from qgis.PyQt.QtGui import QAction  # Qt6
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401


def qenum(cls, enum_name, member):
    """Enum member by scoped name, falling back to the Qt5-style unscoped name."""
    scope = getattr(cls, enum_name, None)
    if scope is not None and hasattr(scope, member):
        return getattr(scope, member)
    return getattr(cls, member)
