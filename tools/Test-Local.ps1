[CmdletBinding()]
param([switch]$SkipBuild, [switch]$SkipControlTool)
$ErrorActionPreference = 'Stop'
$testRoot = Split-Path -Parent $PSScriptRoot
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'Build-Windows.ps1') -SkipControlTool:$SkipControlTool
}
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$testConfig = Get-Content -LiteralPath (Join-Path $SbLocalTools 'windows-environment.json') -Raw | ConvertFrom-Json
$testPython = $testConfig.python
$testBuild = Join-Path $testRoot 'build\windows'
$testEnvNames = @('SBCORE_LIB','SENTRY_BRIDGE_BIN','SENTRY_JNI_LIB','JAVA_HOME','PATH','PYTHONDONTWRITEBYTECODE','TEMP','TMP')
$testPriorEnvironment = @{}
foreach ($testName in $testEnvNames) { $testPriorEnvironment[$testName] = [Environment]::GetEnvironmentVariable($testName, 'Process') }
try {
    $env:SBCORE_LIB = Join-Path $testBuild 'sbcore.dll'
    $env:SENTRY_BRIDGE_BIN = Join-Path $testBuild 'sentry-bridge.exe'
    $env:SENTRY_JNI_LIB = Join-Path $testBuild 'sbjni.dll'
    $env:JAVA_HOME = (Get-Content -LiteralPath (Join-Path $SbLocalTools 'android-environment.json') -Raw | ConvertFrom-Json).javaHome
    # The fuzz executable uses MSVC's AddressSanitizer DLL in the compiler directory.
    $testVsOutput = & $env:ComSpec /d /c "call `"$($testConfig.vsDevCmd)`" -arch=x64 -host_arch=x64 >nul && set PATH"
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the compiler runtime for sanitizer checks.' }
    $testVsPath = @($testVsOutput | Where-Object { $_ -match '^PATH=' })[0].Substring(5)
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + ';' + $testVsPath
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:TEMP = Join-Path $SbScratch 'tmp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    foreach ($testNative in @($env:SBCORE_LIB, $env:SENTRY_BRIDGE_BIN, $env:SENTRY_JNI_LIB)) {
        if (-not (Test-Path -LiteralPath $testNative)) { throw "Native test build missing: $testNative" }
    }
    & $testPython -c 'import pyModeS, reedsolo'
    if ($LASTEXITCODE -ne 0) { throw 'Install requirements-dev.txt in the configured Python environment.' }
    $testReport = Join-Path $testRoot 'dist\test-report.json'
    & $testPython (Join-Path $PSScriptRoot 'run_tests.py') --report $testReport
    if ($LASTEXITCODE -ne 0) { throw 'Full test suite failed or skipped tests; see dist/test-report.json.' }
    & $testPython (Join-Path $PSScriptRoot 'smoke_bridge.py') $env:SENTRY_BRIDGE_BIN
    if ($LASTEXITCODE -ne 0) { throw 'Native UDP smoke test failed.' }
    $testGui = Join-Path $testRoot 'dist\sentry-bridge-gui.exe'
    if (-not $SkipControlTool) {
        & $testPython (Join-Path $PSScriptRoot 'smoke_ctl.py') (Join-Path $testRoot 'dist\sentry-ctl.exe')
        if ($LASTEXITCODE -ne 0) { throw 'Packaged control-tool smoke test failed.' }
        # The packaged GUI must carry the C core and its diagnostic workers without pyModeS.
        & (Join-Path $PSScriptRoot 'Build-Gui.ps1')
        & $testPython (Join-Path $PSScriptRoot 'smoke_gui.py') $testGui
        if ($LASTEXITCODE -ne 0) { throw 'Packaged GUI smoke test failed.' }
    }
    & (Join-Path $testBuild 'fuzz-core.exe') 100000
    if ($LASTEXITCODE -ne 0) { throw 'AddressSanitizer corruption check failed.' }
    $testSummary = Get-Content -LiteralPath $testReport -Raw | ConvertFrom-Json
    $testSummary | Add-Member -NotePropertyName smokePassed -NotePropertyValue $true
    $testSummary | Add-Member -NotePropertyName sanitizer -NotePropertyValue 'MSVC AddressSanitizer'
    $testSummary | Add-Member -NotePropertyName fuzzIterations -NotePropertyValue 100000
    $testSummary | Add-Member -NotePropertyName nativeSha256 -NotePropertyValue (Get-FileHash -LiteralPath $env:SENTRY_BRIDGE_BIN -Algorithm SHA256).Hash.ToLowerInvariant()
    $testSummary | Add-Member -NotePropertyName controlSmokePassed -NotePropertyValue (-not $SkipControlTool)
    $testSummary | Add-Member -NotePropertyName guiSmokePassed -NotePropertyValue (-not $SkipControlTool)
    if (-not $SkipControlTool) {
        $testSummary | Add-Member -NotePropertyName controlSha256 -NotePropertyValue (Get-FileHash -LiteralPath (Join-Path $testRoot 'dist\sentry-ctl.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
        $testSummary | Add-Member -NotePropertyName guiSha256 -NotePropertyValue (Get-FileHash -LiteralPath $testGui -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $testSummary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $testReport -Encoding UTF8
    Write-Host 'Full suite, native UDP and packaged-tool smoke tests, and 100,000 ASan corruption inputs passed.'
} finally {
    foreach ($testName in $testEnvNames) { [Environment]::SetEnvironmentVariable($testName, $testPriorEnvironment[$testName], 'Process') }
}
