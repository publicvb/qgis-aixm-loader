# Links the plugin folder into a QGIS profile (directory junction, no admin rights needed),
# so edits in this repo are picked up after "Reload plugin" / QGIS restart.
# Usage: .\scripts\install_dev.ps1 [-QgisMajor 4] [-Profile default]
param(
    [int]$QgisMajor = 4,
    [string]$Profile = "default"
)
$source = Join-Path (Split-Path -Parent $PSScriptRoot) "aixm_loader"
$plugins = Join-Path $env:APPDATA "QGIS\QGIS$QgisMajor\profiles\$Profile\python\plugins"
if (-not (Test-Path $plugins)) {
    New-Item -ItemType Directory -Force $plugins | Out-Null
}
$target = Join-Path $plugins "aixm_loader"
if (Test-Path $target) {
    $item = Get-Item $target -Force
    if ($item.LinkType -eq "Junction") {
        Write-Host "Already linked: $target -> $($item.Target)"
        exit 0
    }
    Write-Error "$target exists and is not a junction; remove it first."
    exit 1
}
New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Linked $target -> $source"
Write-Host "Enable 'AIXM Loader' in QGIS: Plugins > Manage and Install Plugins > Installed."
