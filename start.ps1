param(
    [string]$DataDir = "",
    [ValidateRange(1, 65535)][int]$Port = 8766,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
function Quote-ProcessArgument([string]$value) {
    '"' + ($value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}
$executables = @(
    (Join-Path $projectRoot 'LeadWorkbench.exe'),
    (Join-Path $projectRoot 'dist/LeadWorkbench/LeadWorkbench.exe'),
    (Join-Path $projectRoot 'dist/LeadWorkbench.exe')
)
$program = $executables | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($DataDir)) {
    if (-not [string]::IsNullOrWhiteSpace($env:LEADWORKBENCH_HOME)) { $DataDir = $env:LEADWORKBENCH_HOME }
    elseif (-not [string]::IsNullOrWhiteSpace($env:LEADWORKBENCH_DATA_DIR)) { $DataDir = $env:LEADWORKBENCH_DATA_DIR }
    elseif ($program) { $DataDir = Join-Path ([System.IO.Path]::GetDirectoryName($program)) 'data' }
    else { $DataDir = Join-Path $projectRoot 'data' }
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
$null = New-Item -ItemType Directory -Path $DataDir -Force
$arguments = @()
if (-not $program) {
    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($python) { $program = $python.Source; $arguments += '-3.12' }
    else {
        $python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $python) { throw 'Install Python 3.12, or use the packaged LeadWorkbench.exe.' }
        $program = $python.Source
    }
    $arguments += @('-m', 'leadworkbench.launcher')
}
$arguments += @('desktop', '--data-dir', $DataDir, '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
$argumentLine = ($arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join ' '
$helper = Start-Process -FilePath $program -ArgumentList $argumentLine -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $DataDir 'launcher-stdout.log') -RedirectStandardError (Join-Path $DataDir 'launcher-stderr.log')
if ($helper.WaitForExit(30000) -and $helper.ExitCode -ne 0) { throw 'Startup failed. Check data/launcher-stderr.log.' }
Write-Output '获客平台已启动。普通启动显示原生桌面窗口；只启服务模式供 CLI/MCP 使用。'
