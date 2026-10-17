[CmdletBinding()]
param([string]$Python, [string]$CMake, [string]$Ninja, [string]$VsDevCmd, [switch]$SkipControlTool)
$ErrorActionPreference = 'Stop'
$winRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$winTools = $SbLocalTools
$winConfigPath = Join-Path $winTools 'windows-environment.json'
$winConfig = if (Test-Path -LiteralPath $winConfigPath) { Get-Content -LiteralPath $winConfigPath -Raw | ConvertFrom-Json } else { $null }
# Record tool paths on first setup or when given explicitly; otherwise leave the shared folder unchanged.
$winSaveConfig = -not $winConfig -or $Python -or $CMake -or $Ninja -or $VsDevCmd
if (-not $Python -and $winConfig) { $Python = $winConfig.python }
if (-not $Python) {
    $Python = Join-Path $winRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
}
if (-not $CMake -and $winConfig) { $CMake = $winConfig.cmake }
if (-not $CMake) {
    $CMake = Join-Path $winTools 'android-sdk\cmake\3.22.1\bin\cmake.exe'
    if (-not (Test-Path -LiteralPath $CMake)) { $CMake = (Get-Command cmake -ErrorAction Stop).Source }
}
if (-not $Ninja -and $winConfig) { $Ninja = $winConfig.ninja }
if (-not $Ninja) {
    $Ninja = Join-Path (Split-Path -Parent $CMake) 'ninja.exe'
    if (-not (Test-Path -LiteralPath $Ninja)) { $Ninja = (Get-Command ninja -ErrorAction Stop).Source }
}
if (-not $VsDevCmd -and $winConfig) { $VsDevCmd = $winConfig.vsDevCmd }
if (-not $VsDevCmd) {
    $winVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path -LiteralPath $winVsWhere)) { throw 'Visual Studio C++ Build Tools are required; supply -VsDevCmd.' }
    $winVsInstall = & $winVsWhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (-not $winVsInstall) { throw 'No Visual Studio installation with the C++ compiler was found.' }
    $VsDevCmd = Join-Path $winVsInstall 'Common7\Tools\VsDevCmd.bat'
}
foreach ($winExe in @($Python, $CMake, $Ninja, $VsDevCmd)) {
    if (-not (Test-Path -LiteralPath $winExe -PathType Leaf)) { throw "Required tool not found: $winExe" }
}
if ($winSaveConfig) {
    New-Item -ItemType Directory -Path $winTools -Force | Out-Null
    @{ python = $Python; cmake = $CMake; ninja = $Ninja; vsDevCmd = $VsDevCmd } | ConvertTo-Json |
        Set-Content -LiteralPath $winConfigPath -Encoding UTF8
}
$winEnvNames = @('PATH','INCLUDE','LIB','LIBPATH','VCToolsInstallDir','WindowsSdkDir','WindowsSDKVersion','JAVA_HOME','PYTHONDONTWRITEBYTECODE','PYINSTALLER_CONFIG_DIR','TEMP','TMP')
$winPriorEnvironment = @{}
foreach ($winName in $winEnvNames) { $winPriorEnvironment[$winName] = [Environment]::GetEnvironmentVariable($winName, 'Process') }
try {
    $winVsOutput = & $env:ComSpec /d /c "call `"$VsDevCmd`" -arch=x64 -host_arch=x64 >nul && set"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to load the Visual Studio build environment.' }
    foreach ($winLine in $winVsOutput) {
        if ($winLine -match '^(PATH|INCLUDE|LIB|LIBPATH|VCToolsInstallDir|WindowsSdkDir|WindowsSDKVersion)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
    $winAndroidConfig = Join-Path $winTools 'android-environment.json'
    if (Test-Path -LiteralPath $winAndroidConfig) { $env:JAVA_HOME = (Get-Content -LiteralPath $winAndroidConfig -Raw | ConvertFrom-Json).javaHome }
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $SbScratch 'pyinstaller-cache'
    $env:TEMP = Join-Path $SbScratch 'tmp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    $winBuild = Join-Path $winRoot 'build\windows'
    & $CMake -S $winRoot -B $winBuild -G Ninja -DCMAKE_BUILD_TYPE=Release "-DCMAKE_MAKE_PROGRAM=$Ninja" -DSB_FUZZ_ASAN=ON
    if ($LASTEXITCODE -ne 0) { throw 'Windows CMake configuration failed.' }
    & $CMake --build $winBuild
    if ($LASTEXITCODE -ne 0) { throw 'Windows native build failed.' }
    $winDist = Join-Path $winRoot 'dist'
    New-Item -ItemType Directory -Path $winDist -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $winBuild 'sentry-bridge.exe') -Destination $winDist -Force
    # These import tables must not require an installed Visual C++ runtime.
    . (Join-Path $PSScriptRoot 'Native-Command.ps1')
    $winDump = Invoke-CapturedNative -FilePath (Get-Command dumpbin).Source -Arguments @('/DEPENDENTS', (Join-Path $winDist 'sentry-bridge.exe'))
    if ($winDump.ExitCode -ne 0 -or ($winDump.Output -join ' ') -match '(?i)(VCRUNTIME|MSVCP|ucrtbase)[\w.-]*\.dll') {
        throw 'Could not verify the native bridge has no Visual C++ runtime dependency.'
    }
    if (-not $SkipControlTool) {
        & $Python -c 'import PyInstaller'
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller is missing. Install requirements-build.txt in the configured Python environment.' }
        & $Python (Join-Path $winRoot "tools/legal_assets.py") --check --python-runtime
        if ($LASTEXITCODE -ne 0) { throw 'Legal asset/runtime verification failed.' }
        & $Python -m PyInstaller --noconfirm --onefile --console --name sentry-ctl --add-data "$(Join-Path $winRoot 'NOTICE.txt');." --add-data "$(Join-Path $winRoot 'notices');notices" --add-data "$(Join-Path $winRoot 'LICENSE');." --icon (Join-Path $winRoot 'assets\icon\sentry-bridge.ico') --distpath $winDist --workpath (Join-Path $winRoot 'build\pyinstaller') --specpath (Join-Path $winRoot 'build\pyinstaller') (Join-Path $winRoot 'sentry_ctl.py')
        if ($LASTEXITCODE -ne 0) { throw 'Control-tool packaging failed.' }
        & (Join-Path $winDist 'sentry-ctl.exe') --help
        if ($LASTEXITCODE -ne 0) { throw 'Packaged control tool did not start.' }
    }
    Write-Host "Windows build verified: $winDist"
} finally {
    foreach ($winName in $winEnvNames) { [Environment]::SetEnvironmentVariable($winName, $winPriorEnvironment[$winName], 'Process') }
}
