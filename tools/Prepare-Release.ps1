[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Tag)
$ErrorActionPreference = 'Stop'
$releaseRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$releaseConfig = Get-Content -LiteralPath (Join-Path $SbLocalTools 'windows-environment.json') -Raw | ConvertFrom-Json
& $releaseConfig.python (Join-Path $PSScriptRoot 'check_version.py') $Tag
if ($LASTEXITCODE -ne 0) { throw 'Update both application versions before preparing this tag.' }
# Before the long build: a checkout that converts line endings can never match the GitHub commit.
& $releaseConfig.python (Join-Path $PSScriptRoot 'release_bundle.py') check-checkout
if ($LASTEXITCODE -ne 0) { throw 'Prepare the release from an LF export of the commit (git -c core.autocrlf=false archive).' }
& (Join-Path $PSScriptRoot 'Build-Android.ps1')
& (Join-Path $PSScriptRoot 'Test-Local.ps1')
& $releaseConfig.python (Join-Path $PSScriptRoot 'release_bundle.py') prepare $Tag
if ($LASTEXITCODE -ne 0) { throw 'Release bundle did not pass validation.' }
Write-Host 'Release bundle prepared locally. Publication is a separate explicit command.'
