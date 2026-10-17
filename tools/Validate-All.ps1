# Full validation on one Windows machine: Windows build and tests, the signed APK, and Linux in WSL.
# Run it from a configured clone.
#   .\tools\Validate-All.ps1                 # everything
#   .\tools\Validate-All.ps1 -SkipAndroid    # skip the APK build
#   .\tools\Validate-All.ps1 -SkipLinux      # skip WSL
# WSL needs CMake, a C compiler, JDK 17+ and a Python with requirements-dev.txt installed;
# pass -WslPython (a path inside WSL) or set SB_WSL_PYTHON when that is not python3.
[CmdletBinding()]
param([switch]$SkipAndroid, [switch]$SkipLinux, [string]$WslPython = $env:SB_WSL_PYTHON)
$ErrorActionPreference = 'Stop'
$allRoot = Split-Path -Parent $PSScriptRoot
$allDist = Join-Path $allRoot 'dist'
New-Item -ItemType Directory -Path $allDist -Force | Out-Null
$allSummaryPath = Join-Path $allDist 'validation-summary.json'
$allSummary = [ordered]@{ commit = $null; windows = 'not run'; android = 'not run'; linux = 'not run'; passed = $false }
$allSummary | ConvertTo-Json | Set-Content -LiteralPath $allSummaryPath -Encoding UTF8
try {
    $allCommit = & git -C $allRoot rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0) { $allSummary.commit = "$allCommit".Trim() }
} catch { }  # the commit is informational; Windows PowerShell 5.1 can raise on native stderr

function Save-Summary { $allSummary | ConvertTo-Json | Set-Content -LiteralPath $allSummaryPath -Encoding UTF8 }

try {
    $allSummary.windows = 'failed'
    & (Join-Path $PSScriptRoot 'Test-Local.ps1')
    $allSummary.windows = 'passed'
    Save-Summary

    if ($SkipAndroid) { $allSummary.android = 'skipped' } else {
        $allSummary.android = 'failed'
        & (Join-Path $PSScriptRoot 'Build-Android.ps1') -NoCounterUpdate
        $allSummary.android = 'passed'
    }
    Save-Summary

    if ($SkipLinux) { $allSummary.linux = 'skipped' } else {
        $allSummary.linux = 'failed'
        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw 'WSL is not installed; pass -SkipLinux.' }
        if (-not $WslPython) { $WslPython = 'python3' }
        # Keep the Linux report separate from the Windows test report.
        & wsl.exe --cd $allRoot -- $WslPython tools/test_posix.py --report dist/posix-test-report.json
        if ($LASTEXITCODE -ne 0) { throw 'Linux validation in WSL failed; see dist/posix-test-report.json.' }
        $allSummary.linux = 'passed'
    }
    $allSummary.passed = $true
    Save-Summary
    Write-Host "Validation passed: Windows $($allSummary.windows), Android $($allSummary.android), Linux $($allSummary.linux)."
} catch {
    Save-Summary
    throw
}
