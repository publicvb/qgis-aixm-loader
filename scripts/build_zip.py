"""Builds the upload zip for plugins.qgis.org from the committed aixm_loader/ folder.

Uses `git archive`, so the zip contains exactly what is in the repository at HEAD
(no __pycache__, tests or sample data) and uncommitted changes are refused.
Usage: python scripts/build_zip.py   ->  dist/aixm_loader-<version>.zip
"""
import configparser
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = "aixm_loader"
# python-qgis.bat replaces PATH, so fall back to the default Git for Windows location
GIT = shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe"


def main():
    dirty = subprocess.run([GIT, "status", "--porcelain", "--", PLUGIN], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout
    if dirty.strip():
        sys.exit(f"{PLUGIN}/ has uncommitted changes; commit them first:\n{dirty}")

    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, PLUGIN, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{PLUGIN}-{version}.zip")
    subprocess.run([GIT, "archive", "--format=zip", "-o", out, "HEAD", PLUGIN],
                   cwd=ROOT, check=True)

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    for required in ("metadata.txt", "__init__.py", "LICENSE"):
        if f"{PLUGIN}/{required}" not in names:
            sys.exit(f"{required} missing from the zip")
    print(f"{out} ({len(names)} entries)")


if __name__ == "__main__":
    main()
