param(
    [string]$Python = 'python',
    [string]$BuildRoot = '',
    [switch]$OneFile,
    [switch]$DesktopLauncher
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if ($OneFile -and $DesktopLauncher) { throw 'The native desktop requires the two onedir distributions. Omit -OneFile when using -DesktopLauncher.' }
$entry = Join-Path $projectRoot 'leadworkbench/launcher.py'
$static = Join-Path $projectRoot 'static'
$certs = Join-Path $projectRoot 'certs'
$icon = Join-Path $static 'app.ico'
$versionInfo = Join-Path $projectRoot 'windows-version.txt'
if ([string]::IsNullOrWhiteSpace($BuildRoot)) { $BuildRoot = Join-Path $projectRoot 'build' }
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$staging = Join-Path $BuildRoot 'distribution'
$destination = Join-Path $projectRoot 'dist'
if (-not (Test-Path -LiteralPath $entry) -or -not (Test-Path -LiteralPath $static)) { throw 'Run from the complete project folder.' }
& $Python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) { throw 'Install the optional build dependency first: python -m pip install pyinstaller. Install requirements-desktop.txt for the native desktop.' }
$common = @('--noconfirm', '--clean', '--paths', $projectRoot, '--collect-submodules', 'leadworkbench',
    '--add-data', ($static + ';static'), '--add-data', ($certs + ';certs'), '--distpath', $staging,
    '--workpath', $BuildRoot, '--specpath', $BuildRoot)
if (-not (Test-Path -LiteralPath $icon) -or -not (Test-Path -LiteralPath $versionInfo)) { throw 'Application icon and Windows version metadata are required.' }
$common += @('--icon', $icon, '--version-file', $versionInfo)
if ($OneFile) { $common += '--onefile' } else { $common += '--onedir' }
& $Python -m PyInstaller @common --name LeadWorkbench --console --exclude-module PySide6 $entry
if ($LASTEXITCODE -ne 0) { throw 'Console executable build failed.' }
if ($DesktopLauncher) {
    & $Python -c "import PySide6.QtWidgets, PySide6.QtNetwork"
    if ($LASTEXITCODE -ne 0) { throw 'Install requirements-desktop.txt in the project build environment first.' }
    $native = @('--hidden-import', 'PySide6.QtNetwork', '--hidden-import', 'PySide6.support.deprecated', '--exclude-module', 'PySide6.QtWebEngineCore', '--exclude-module', 'PySide6.QtWebEngineWidgets',
        '--exclude-module', 'PySide6.QtQml', '--exclude-module', 'PySide6.QtQuick', '--exclude-module', 'PySide6.QtQuickWidgets',
        '--exclude-module', 'PyQt6', '--exclude-module', 'PyQt5', '--exclude-module', 'PySide2')
    & $Python -m PyInstaller @common @native --name LeadWorkbenchDesktop --windowed $entry
    if ($LASTEXITCODE -ne 0) { throw 'Optional desktop launcher build failed.' }
}
$null = New-Item -ItemType Directory -Path $destination -Force
$names = @('LeadWorkbench')
if ($DesktopLauncher) { $names += 'LeadWorkbenchDesktop' }
foreach ($name in $names) {
    $noticeTarget = $destination
    if ($OneFile) {
        Copy-Item -LiteralPath (Join-Path $staging ($name + '.exe')) -Destination (Join-Path $destination ($name + '.exe')) -Force
    } else {
        $target = Join-Path $destination $name
        $noticeTarget = $target
        $null = New-Item -ItemType Directory -Path $target -Force
        # Build into staging, then merge only binaries/resources. PyInstaller
        # must never remove the existing data directory during an upgrade.
        foreach ($item in Get-ChildItem -LiteralPath (Join-Path $staging $name) -Force) {
            Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force
        }
    }
    $licenseTarget = Join-Path $noticeTarget 'licenses'
    $null = New-Item -ItemType Directory -Path $licenseTarget -Force
    foreach ($licenseItem in Get-ChildItem -LiteralPath (Join-Path $projectRoot 'licenses') -Force) {
        Copy-Item -LiteralPath $licenseItem.FullName -Destination $licenseTarget -Recurse -Force
    }
    foreach ($noticeName in @('LICENSE', 'NOTICE.md')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $noticeName) -Destination (Join-Path $noticeTarget $noticeName) -Force
    }
}
Write-Output 'Build complete. LeadWorkbenchDesktop is the native Qt Widgets app. LeadWorkbench provides the local service, CLI and MCP. Keep both onedir distributions intact.'
