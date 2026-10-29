# Runs all tests with the Python interpreter bundled with QGIS.
# Usage: .\scripts\run_tests.ps1 [-QgisRoot "C:\Program Files\QGIS 4.2.3"]
param(
    [string]$QgisRoot = (Get-ChildItem "C:\Program Files" -Directory -Filter "QGIS*" |
        Sort-Object Name -Descending | Select-Object -First 1).FullName
)
$repo = Split-Path -Parent $PSScriptRoot
$env:QT_QPA_PLATFORM = "offscreen"
Push-Location $repo
try {
    & (Join-Path $QgisRoot "bin\python-qgis.bat") -m unittest discover -s tests -t . -v
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
