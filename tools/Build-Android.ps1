# Default: use only local dependencies. Use -Online in a normal terminal when refreshing dependencies.
[CmdletBinding()]
param(
    [switch]$Online,
    [ValidateRange(0, 2100000000)][int]$VersionCode = 0,
    # Validation builds: use the next version code but do not record it, so test builds
    # do not use up codes that releases need.
    [switch]$NoCounterUpdate
)
$ErrorActionPreference = 'Stop'
$buildRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$buildTools = $SbLocalTools
$buildEnvironmentFile = Join-Path $buildTools 'android-environment.json'
if (-not (Test-Path -LiteralPath $buildEnvironmentFile)) { throw 'Run tools\Setup-Android.ps1 in a normal PowerShell window first.' }
$buildEnvironment = Get-Content -LiteralPath $buildEnvironmentFile -Raw | ConvertFrom-Json
# The tracked counter is the record. This checkout's build\local counter also covers release builds
# not committed yet. The tool folder's counter is only read: SB_LOCAL_TOOLS may belong to another
# clone, and builds never write there.
$buildCounterFile = Join-Path $SbScratch 'android-version-code.txt'
$buildTrackedCounter = Join-Path $buildRoot 'android\version-code.txt'
$buildPreviousCode = 32  # Published v0.1.2 APK uses 32.
foreach ($buildCounter in @($buildTrackedCounter, $buildCounterFile, (Join-Path $buildTools 'android-version-code.txt'))) {
    if (Test-Path -LiteralPath $buildCounter) {
        $buildPreviousCode = [Math]::Max($buildPreviousCode, [int](Get-Content -LiteralPath $buildCounter -Raw).Trim())
    }
}
if ($VersionCode -eq 0) {
    if ($buildPreviousCode -ge 2100000000) { throw 'Android version-code range exhausted.' }
    $VersionCode = $buildPreviousCode + 1
}
if ($VersionCode -le $buildPreviousCode) { throw "Version code must exceed $buildPreviousCode for an update build." }
$buildEnvNames = @('JAVA_HOME','ANDROID_HOME','ANDROID_SDK_ROOT','ANDROID_USER_HOME','ANDROID_SDK_HOME','GRADLE_USER_HOME','SB_ANDROID_VERSION_CODE',
    'SB_KEYSTORE_FILE','SB_KEYSTORE_PASSWORD','SB_KEY_PASSWORD','SB_KEY_ALIAS','SB_REQUIRE_RELEASE_SIGNING')
$buildPriorEnvironment = @{}
foreach ($buildEnvName in $buildEnvNames) { $buildPriorEnvironment[$buildEnvName] = [Environment]::GetEnvironmentVariable($buildEnvName, 'Process') }
try {
    $env:JAVA_HOME = $buildEnvironment.javaHome
    $env:ANDROID_HOME = $buildEnvironment.sdkRoot
    $env:ANDROID_SDK_ROOT = $buildEnvironment.sdkRoot
    $env:ANDROID_USER_HOME = Join-Path $SbScratch '.android'
    $env:ANDROID_SDK_HOME = $SbScratch  # Older Android tools append .android here.
    New-Item -ItemType Directory -Path $env:ANDROID_USER_HOME -Force | Out-Null
    $env:GRADLE_USER_HOME = $buildEnvironment.gradleUserHome
    $env:SB_ANDROID_VERSION_CODE = [string]$VersionCode
    . (Join-Path $buildRoot 'tools\Import-Signing.ps1')
    # Gradle's Windows ZIP cache can retain undeletable temporary directories between builds.
    # Remove only this generated package cache, after verifying its workspace location.
    $buildPackageCache = Join-Path $buildRoot 'android\app\build\intermediates\incremental\packageRelease'
    if (Test-Path -LiteralPath $buildPackageCache) {
        $buildResolvedCache = (Resolve-Path -LiteralPath $buildPackageCache).Path
        $buildAllowedCache = [IO.Path]::GetFullPath((Join-Path $buildRoot 'android\app\build')).TrimEnd('\') + '\'
        if (-not $buildResolvedCache.StartsWith($buildAllowedCache, [StringComparison]::OrdinalIgnoreCase)) { throw 'Android package cache is outside the generated build directory.' }
        Remove-Item -LiteralPath $buildResolvedCache -Recurse -Force
    }
    $buildGradle = Join-Path $buildEnvironment.gradleHome 'bin\gradle.bat'
    $buildGradleArguments = @('--no-daemon','--console=plain','--project-dir',(Join-Path $buildRoot 'android'))
    if (-not $Online) { $buildGradleArguments += '--offline' }
    $buildGradleArguments += 'assembleRelease'
    & $buildGradle @buildGradleArguments
    if ($LASTEXITCODE -ne 0) { throw 'Gradle build failed; no APK was copied to dist.' }

    $buildApk = Join-Path $buildRoot 'android\app\build\outputs\apk\release\app-release.apk'
    if (-not (Test-Path -LiteralPath $buildApk)) { throw 'Gradle did not produce the release APK.' }
    $buildAndroidTools = Join-Path $env:ANDROID_HOME ('build-tools\' + $buildEnvironment.buildToolsVersion)
    $buildSignatureResult = Invoke-CapturedNative -FilePath (Join-Path $buildAndroidTools 'apksigner.bat') -Arguments @('verify', '--verbose', '--print-certs', $buildApk)
    if ($buildSignatureResult.ExitCode -ne 0) { throw 'APK signature verification failed.' }
    $buildSignature = $buildSignatureResult.Output
    $buildSigners = @([regex]::Matches(($buildSignature -join [Environment]::NewLine), 'certificate SHA-256 digest:\s*([a-fA-F0-9]{64})') | ForEach-Object {
        $_.Groups[1].Value.ToLowerInvariant()
    } | Sort-Object -Unique)
    if ($buildSigners.Count -ne 1 -or $buildSigners[0] -ne $signExpected) { throw 'APK is not signed with the original release certificate.' }

    $buildBadgingResult = Invoke-CapturedNative -FilePath (Join-Path $buildAndroidTools 'aapt2.exe') -Arguments @('dump', 'badging', $buildApk)
    if ($buildBadgingResult.ExitCode -ne 0) { throw 'Could not inspect APK metadata.' }
    $buildBadging = $buildBadgingResult.Output
    $buildVersionText = Get-Content -LiteralPath (Join-Path $buildRoot 'android\app\build.gradle.kts') -Raw
    $buildVersionName = [regex]::Match($buildVersionText, 'versionName\s*=\s*"([^"]+)"').Groups[1].Value
    $buildPackageLine = @($buildBadging | Where-Object { $_ -match '^package:' }) -join ''
    if ($buildPackageLine -notmatch [regex]::Escape("versionCode='$VersionCode'") -or
        $buildPackageLine -notmatch [regex]::Escape("versionName='$buildVersionName'") -or
        $buildPackageLine -notmatch "name='io.github.tehcnological.sentrybridge'") {
        throw 'APK identity or version does not match the requested build.'
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $buildZip = [IO.Compression.ZipFile]::OpenRead($buildApk)
    try {
        $buildKotlin = Get-Content -LiteralPath (Join-Path $buildRoot 'android\app\src\main\java\io\github\tehcnological\sentrybridge\SbCore.kt') -Raw
        $buildExpectedJni = @([regex]::Matches($buildKotlin, 'external fun ([A-Za-z0-9]+)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
        $buildReadElf = Join-Path $env:ANDROID_HOME ('ndk\' + $buildEnvironment.ndkVersion + '\toolchains\llvm\prebuilt\windows-x86_64\bin\llvm-readelf.exe')
        foreach ($buildAbi in @('arm64-v8a','armeabi-v7a','x86_64')) {
            $buildLibEntry = $buildZip.GetEntry("lib/$buildAbi/libsbjni.so")
            if (-not $buildLibEntry) { throw "APK is missing native library for $buildAbi." }
            $buildAbiDir = Join-Path $SbScratch "apk-check\$buildAbi"
            New-Item -ItemType Directory -Path $buildAbiDir -Force | Out-Null
            $buildLibPath = Join-Path $buildAbiDir 'libsbjni.so'
            [IO.Compression.ZipFileExtensions]::ExtractToFile($buildLibEntry, $buildLibPath, $true)
            $buildSymbolsResult = Invoke-CapturedNative -FilePath $buildReadElf -Arguments @('-Ws', '--dyn-syms', $buildLibPath)
            if ($buildSymbolsResult.ExitCode -ne 0) { throw "Cannot inspect JNI exports for $buildAbi." }
            $buildSymbols = $buildSymbolsResult.Output
            $buildActualJni = @([regex]::Matches(($buildSymbols -join [Environment]::NewLine),
                'Java_io_github_tehcnological_sentrybridge_SbCore_([A-Za-z0-9]+)') | ForEach-Object {
                    $_.Groups[1].Value
                } | Sort-Object -Unique)
            if (($buildActualJni -join ' ') -ne ($buildExpectedJni -join ' ')) { throw "JNI exports differ from Kotlin for $buildAbi." }
        }
    } finally { $buildZip.Dispose() }

    $buildDist = Join-Path $buildRoot 'dist'
    New-Item -ItemType Directory -Path $buildDist -Force | Out-Null
    $buildOutput = Join-Path $buildDist 'sentry-bridge.apk'
    Copy-Item -LiteralPath $buildApk -Destination $buildOutput -Force
    $buildApkHash = (Get-FileHash -LiteralPath $buildOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    ($buildApkHash + '  sentry-bridge.apk') | Set-Content -LiteralPath (Join-Path $buildDist 'SHA256SUMS') -Encoding ASCII
    if (-not $NoCounterUpdate) {
        [string]$VersionCode | Set-Content -LiteralPath $buildCounterFile -Encoding ASCII
        [string]$VersionCode | Set-Content -LiteralPath $buildTrackedCounter -Encoding ASCII
    }
    $buildPython = if (Test-Path -LiteralPath (Join-Path $buildTools 'windows-environment.json')) {
        (Get-Content -LiteralPath (Join-Path $buildTools 'windows-environment.json') -Raw | ConvertFrom-Json).python
    } elseif (Test-Path -LiteralPath (Join-Path $buildRoot '.venv\Scripts\python.exe')) {
        Join-Path $buildRoot '.venv\Scripts\python.exe'
    } else { (Get-Command python -ErrorAction Stop).Source }
    $buildSources = & $buildPython -c 'import json,sys; sys.path.insert(0,sys.argv[1]); from release_bundle import source_snapshot; print(json.dumps(source_snapshot()))' $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not record APK build sources.' }
    @{
        versionName = $buildVersionName
        versionCode = $VersionCode
        signerSha256 = $buildSigners[0]
        apkSha256 = $buildApkHash
        checkedAbis = @('arm64-v8a','armeabi-v7a','x86_64')
        dependencyMode = $(if ($Online) { 'online' } else { 'offline' })
        sources = ($buildSources -join [Environment]::NewLine | ConvertFrom-Json)
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $buildDist 'android-build-report.json') -Encoding UTF8
    Write-Host "Verified signed APK v$buildVersionName (version code $VersionCode): $buildOutput"
    Write-Host 'Certificate, package/version, and JNI exports in all three ABIs passed.'
} finally {
    foreach ($buildEnvName in $buildEnvNames) {
        [Environment]::SetEnvironmentVariable($buildEnvName, $buildPriorEnvironment[$buildEnvName], 'Process')
    }
}
