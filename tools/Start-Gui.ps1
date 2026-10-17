# Double-click Run-Gui.cmd instead of changing PowerShell's execution policy.
# -Port and -DataDir default to gui_server.py's own defaults; tests point them at a test server.
param([int]$Port = 8766, [string]$DataDir = '')
$ErrorActionPreference = 'Stop'
$guiRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
if (-not $DataDir) { $DataDir = Join-Path $guiRoot '.local-tools\gui-data' }

function Show-GuiError([string]$Message) {
    if ($env:SB_GUI_HEADLESS) { [Console]::Error.WriteLine($Message); return }  # tests: no dialog
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Message, 'Sentry Bridge') | Out-Null
}

$guiEnvironment = Join-Path $SbLocalTools 'windows-environment.json'
if (Test-Path -LiteralPath $guiEnvironment) {
    $guiPython = (Get-Content -LiteralPath $guiEnvironment -Raw | ConvertFrom-Json).python
} elseif (Test-Path -LiteralPath (Join-Path $guiRoot '.venv\Scripts\python.exe')) {
    $guiPython = Join-Path $guiRoot '.venv\Scripts\python.exe'
} else {
    $guiPython = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $guiPython)) { throw 'Python is unavailable. Check the local installation.' }
$guiUrl = "http://127.0.0.1:$Port"
$guiRunning = $false
try {
    # A running instance saved its session token in its data folder.
    $guiToken = (Get-Content -LiteralPath (Join-Path $DataDir 'gui-session') -Raw -ErrorAction Stop).Trim()
    $guiExisting = Invoke-RestMethod -Uri "$guiUrl/api/status" -Headers @{ Authorization = "Bearer $guiToken" } -TimeoutSec 2 -ErrorAction Stop
    $guiRunning = $guiExisting.adapter -eq 'local'
} catch { }  # No running instance with this token; launch one below.
if ($guiRunning) {
    # It runs. A failure from here on is reported, never handled by starting a second instance:
    # that would replace the saved token the running instance still uses.
    try {
        # A one-time launch URL: the token itself must not reach a browser command line.
        $guiLaunch = Invoke-RestMethod -Method Post -Uri "$guiUrl/api/launch" -Headers @{ Authorization = "Bearer $guiToken" } -TimeoutSec 5 -ErrorAction Stop
        if ($env:SB_GUI_HEADLESS) { $guiLaunch.url } else { Start-Process $guiLaunch.url }  # tests: no browser
        exit 0
    } catch {
        $guiDetail = $_.Exception.Message
        try { $guiDetail = ($_.ErrorDetails.Message | ConvertFrom-Json).error } catch { }
        Show-GuiError "Sentry Bridge is already running but did not open a window: $guiDetail"
        exit 1
    }
}
$guiOpen = if ($env:SB_GUI_HEADLESS) { @() } else { @('--open') }
& $guiPython (Join-Path $guiRoot 'tools\gui_server.py') @guiOpen --port $Port --data-dir $DataDir
if ($LASTEXITCODE -ne 0) {
    Show-GuiError 'Sentry Bridge could not start. Close another copy or check the local installation.'
    exit 1
}
