param(
    [string]$DataDir = '',
    [ValidateRange(1, 65535)][int]$Port = 8766
)
$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$desktopLauncher = Join-Path $projectRoot 'dist/LeadWorkbenchDesktop/LeadWorkbenchDesktop.exe'
$mainExecutable = Join-Path $projectRoot 'dist/LeadWorkbench/LeadWorkbench.exe'
$program = if (Test-Path -LiteralPath $desktopLauncher -PathType Leaf) { $desktopLauncher } else { $mainExecutable }
if (-not (Test-Path -LiteralPath $program -PathType Leaf)) { throw 'Missing packaged application. Keep the complete dist folder.' }
$icon = Join-Path $projectRoot 'static/app.ico'
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw 'Missing application icon.' }
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $projectRoot 'dist/LeadWorkbench/data' }
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
if ($DataDir.Contains('"')) { throw 'Invalid data directory.' }
$desktopDirectory = [Environment]::GetFolderPath('DesktopDirectory')
if ([string]::IsNullOrWhiteSpace($desktopDirectory)) { throw 'Desktop directory is not available.' }
$shortcutPath = Join-Path $desktopDirectory '获客平台.lnk'
$shortcutShell = New-Object -ComObject WScript.Shell
$shortcut = $shortcutShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $program
$shortcut.Arguments = 'desktop --data-dir "' + $DataDir + '" --port ' + $Port
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = $icon + ',0'
$shortcut.Description = '获客平台 · 采集线索与审核证据'
$shortcut.WindowStyle = 1
$shortcut.Save()
if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) { throw 'Shortcut was not created.' }
Write-Output ('已创建桌面快捷方式：' + $shortcutPath)
