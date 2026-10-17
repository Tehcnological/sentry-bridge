# Run in a normal PowerShell window with Internet access.
# Installs tools within this project (or SB_LOCAL_TOOLS) and builds the first permanently signed APK.
[CmdletBinding()]
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$setupRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$setupTools = $SbLocalTools  # setup is the one step that installs into this folder
$setupDownloads = Join-Path $setupTools 'downloads'
$setupJdkDir = Join-Path $setupTools 'jdk'
$setupGradleDir = Join-Path $setupTools 'gradle'
$setupSdk = Join-Path $setupTools 'android-sdk'
foreach ($setupDir in @($setupDownloads, $setupJdkDir, $setupGradleDir, $setupSdk)) {
    New-Item -ItemType Directory -Path $setupDir -Force | Out-Null
}
function Get-VerifiedArchive {
    param([string]$Uri, [string]$Checksum, [string]$Destination)
    if ($Checksum -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid vendor checksum.' }
    if (Test-Path -LiteralPath $Destination) {
        if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ieq $Checksum) { return }
    }
    Write-Host ('Downloading ' + [IO.Path]::GetFileName($Destination))
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination -TimeoutSec 1800
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ine $Checksum) {
        throw ('Checksum mismatch: ' + [IO.Path]::GetFileName($Destination))
    }
}

# Keep JDK 17 for the same Java version as the release workflow.
$setupJdks = @(Get-ChildItem -LiteralPath $setupJdkDir -Directory | Where-Object {
    $_.Name -like 'jdk-17*' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe'))
})
if ($setupJdks.Count -eq 0) {
    $setupAssetsUrl = 'https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&jvm_impl=hotspot&os=windows&vendor=eclipse'
    $setupAssets = @(Invoke-RestMethod -Uri $setupAssetsUrl -TimeoutSec 60)
    if ($setupAssets.Count -eq 0) { throw 'Adoptium returned no Windows x64 JDK 17 archive.' }
    $setupPackage = $setupAssets[0].binary.package
    if ([uri]$setupPackage.link -and ([uri]$setupPackage.link).Scheme -ne 'https') { throw 'Non-HTTPS JDK download rejected.' }
    $setupJdkZip = Join-Path $setupDownloads $setupPackage.name
    Get-VerifiedArchive $setupPackage.link $setupPackage.checksum $setupJdkZip
    Expand-Archive -LiteralPath $setupJdkZip -DestinationPath $setupJdkDir -Force
    $setupJdks = @(Get-ChildItem -LiteralPath $setupJdkDir -Directory | Where-Object {
        $_.Name -like 'jdk-17*' -and (Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe'))
    })
}
if ($setupJdks.Count -ne 1) { throw 'Expected exactly one local JDK 17 directory.' }
$setupJavaHome = $setupJdks[0].FullName
$env:JAVA_HOME = $setupJavaHome
& (Join-Path $setupJavaHome 'bin\java.exe') -version
if ($LASTEXITCODE -ne 0) { throw 'JDK verification failed.' }

$setupGradleHome = Join-Path $setupGradleDir 'gradle-8.10.2'
if (-not (Test-Path -LiteralPath (Join-Path $setupGradleHome 'bin\gradle.bat'))) {
    $setupGradleZip = Join-Path $setupDownloads 'gradle-8.10.2-bin.zip'
    Get-VerifiedArchive 'https://services.gradle.org/distributions/gradle-8.10.2-bin.zip' '31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26' $setupGradleZip
    Expand-Archive -LiteralPath $setupGradleZip -DestinationPath $setupGradleDir -Force
}

$setupSdkManager = Join-Path $setupSdk 'cmdline-tools\latest\bin\sdkmanager.bat'
if (-not (Test-Path -LiteralPath $setupSdkManager)) {
    Write-Host 'Google requires acceptance of the Android SDK License Agreement before downloading:'
    Write-Host 'https://developer.android.com/studio#terms-and-conditions'
    $setupLicenseAnswer = Read-Host 'After reading the agreement, type ACCEPT if you agree (anything else stops setup)'
    if ($setupLicenseAnswer -cne 'ACCEPT') { throw 'Android SDK agreement was not accepted; setup stopped.' }
    $setupSdkZip = Join-Path $setupDownloads 'commandlinetools-win-15859902_latest.zip'
    Get-VerifiedArchive 'https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip' '90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a' $setupSdkZip
    $setupSdkExtract = Join-Path $setupTools 'sdk-extract'
    New-Item -ItemType Directory -Path $setupSdkExtract -Force | Out-Null
    Expand-Archive -LiteralPath $setupSdkZip -DestinationPath $setupSdkExtract -Force
    $setupCmdlineLatest = Join-Path $setupSdk 'cmdline-tools\latest'
    New-Item -ItemType Directory -Path $setupCmdlineLatest -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $setupSdkExtract 'cmdline-tools') | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $setupCmdlineLatest -Recurse -Force
    }
}

# The installed JDK 23 can run current SDK tools; Gradle uses JDK 17.
$setupSdkJava = 'C:\Program Files\Java\jdk-23'
if (Test-Path -LiteralPath (Join-Path $setupSdkJava 'bin\java.exe')) { $env:JAVA_HOME = $setupSdkJava }
$env:ANDROID_HOME = $setupSdk
$env:ANDROID_SDK_ROOT = $setupSdk
Write-Host 'Review and accept the SDK component licenses at the prompts below.'
& $setupSdkManager "--sdk_root=$setupSdk" --licenses
if ($LASTEXITCODE -ne 0) { throw 'SDK license review did not complete.' }
$setupPackages = @('platform-tools', 'platforms;android-34', 'build-tools;34.0.0', 'ndk;27.0.12077973', 'cmake;3.22.1')
& $setupSdkManager "--sdk_root=$setupSdk" @setupPackages
if ($LASTEXITCODE -ne 0) { throw 'SDK/NDK/CMake installation failed.' }

$setupRequiredFiles = @('platforms\android-34\android.jar', 'build-tools\34.0.0\apksigner.bat',
    'ndk\27.0.12077973\toolchains\llvm\prebuilt\windows-x86_64\bin\llvm-readelf.exe', 'cmake\3.22.1\bin\cmake.exe')
foreach ($setupRequiredFile in $setupRequiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $setupSdk $setupRequiredFile))) {
        throw "Required Android tool is missing: $setupRequiredFile"
    }
}
$setupEnvironment = @{
    javaHome = $setupJavaHome
    gradleHome = $setupGradleHome
    sdkRoot = $setupSdk
    gradleUserHome = (Join-Path $setupTools 'gradle-cache')
    ndkVersion = '27.0.12077973'
    buildToolsVersion = '34.0.0'
}
$setupEnvironment | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $setupTools 'android-environment.json') -Encoding UTF8
('sdk.dir=' + $setupSdk.Replace('\', '/')) | Set-Content -LiteralPath (Join-Path $setupRoot 'android\local.properties') -Encoding ASCII
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'Build-Android.ps1') -Online
    if (-not $?) { throw 'First local APK build failed.' }
    Write-Host 'Setup complete. Future builds can reuse the populated caches offline.'
} else {
    Write-Host 'Tools installed. The first build still needs Internet access to populate Gradle dependencies.'
}
