#requires -Version 5.1
<#
.SYNOPSIS
  One-time: adds "Jellyfin SyncPlay Dashboard" to your Start Menu (All apps).

.DESCRIPTION
  Creates a shortcut in:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\
  so it appears in the Start Menu and is searchable. To also get a Start tile,
  press Win, type "Jellyfin SyncPlay", right-click it -> "Pin to Start".

.EXAMPLE
  pwsh -NoProfile -File .\Add-StartMenuShortcut.ps1
#>

$ErrorActionPreference = 'Stop'

$workdir = $PSScriptRoot
$target  = Join-Path $workdir 'Start-Dashboard.cmd'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

if (-not (Test-Path $target)) {
    Write-Host "Launcher not found: $target" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $programs | Out-Null
$lnk = Join-Path $programs 'Jellyfin SyncPlay Doctor.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $workdir
$sc.WindowStyle      = 1
$sc.Description      = 'Jellyfin SyncPlay Doctor'
# Use the bundled icon.
$iconPath = Join-Path $workdir 'assets\icon.ico'
if (Test-Path $iconPath) {
    $sc.IconLocation = $iconPath
}
$sc.Save()

Write-Host "Added to Start Menu: $lnk" -ForegroundColor Green
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  - Press Win, type "Jellyfin SyncPlay", and launch it from there.'
Write-Host '  - To pin it as a tile: right-click it -> "Pin to Start".'
