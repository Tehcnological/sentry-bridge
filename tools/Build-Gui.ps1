param()
$ErrorActionPreference = 'Stop'
$guiRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$guiEnv = Get-Content -LiteralPath (Join-Path $SbLocalTools 'windows-environment.json') -Raw | ConvertFrom-Json
$guiCore = Join-Path $guiRoot 'build/windows/sbcore.dll'
$guiPriorCache = $env:PYINSTALLER_CONFIG_DIR
if (-not (Test-Path -LiteralPath $guiCore)) { throw 'Build the Windows native core first.' }
Push-Location $guiRoot
try {
    $guiBuild = Join-Path $guiRoot ('build/desktop/' + [Guid]::NewGuid().ToString('N'))
    # The packaged GUI always uses the C core. Keep the Python reference decoder and its optional
    # dependencies out to keep the native package small. The combined app is GPLv3;
    # excluding a Python package does not establish independent decoder provenance.
    $guiModules = @('bridge', 'pyModeS', 'numpy', 'yaml')
    $guiExcluded = @($guiModules | ForEach-Object { '--exclude-module', $_ })
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $SbScratch 'pyinstaller-gui-cache'
    & $guiEnv.python (Join-Path $guiRoot "tools/legal_assets.py") --check --python-runtime
    if ($LASTEXITCODE -ne 0) { throw 'Legal asset/runtime verification failed.' }
    & $guiEnv.python -m PyInstaller --noconfirm --onefile --windowed --name sentry-bridge-gui --icon (Join-Path $guiRoot 'assets\icon\sentry-bridge.ico') --paths $guiRoot --add-data "$(Join-Path $guiRoot 'web');web" --add-data "$(Join-Path $guiRoot 'LICENSE');." --add-data "$(Join-Path $guiRoot 'NOTICE.txt');." --add-data "$(Join-Path $guiRoot 'notices');notices" --add-data "$(Join-Path $guiRoot 'core/sbcore.h');core" --add-binary "$guiCore;." --hidden-import capture --hidden-import tools.check_weather --hidden-import ws_probe @guiExcluded --distpath dist --workpath $guiBuild --specpath $guiBuild tools/desktop_app.py
    if ($LASTEXITCODE -ne 0) { throw 'Windows GUI packaging failed.' }
    $guiToc = Get-ChildItem -LiteralPath $guiBuild -Recurse -Filter 'PYZ-00.toc' | Select-Object -First 1
    if (-not $guiToc) { throw 'Could not inspect the packaged GUI modules.' }
    $guiTocText = Get-Content -LiteralPath $guiToc.FullName -Raw
    foreach ($guiModule in $guiModules) {
        if ($guiTocText -match ("'" + [regex]::Escape($guiModule) + "[.']")) { throw "Packaged GUI includes excluded module $guiModule." }
    }
    $guiExe = Join-Path $guiRoot 'dist/sentry-bridge-gui.exe'
    Write-Host ('Windows GUI built without excluded modules: ' + $guiExe)
    Write-Host ('SHA256 ' + (Get-FileHash -LiteralPath $guiExe -Algorithm SHA256).Hash.ToLowerInvariant())
} finally { $env:PYINSTALLER_CONFIG_DIR = $guiPriorCache; Pop-Location }
