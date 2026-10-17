# Dot-source tools/Import-Signing.ps1; key material stays in ignored local-signing/ (or SB_SIGNING_DIR).
# Loads the original key into this PowerShell session; never prints passwords.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$signKeystore = Join-Path $SbSigningDir 'sentry-bridge-signing.p12'
$signPasswordFile = Join-Path $SbSigningDir 'sentry-bridge-signing.password'
foreach ($signFile in @($signKeystore, $signPasswordFile)) {
    if (-not (Test-Path -LiteralPath $signFile -PathType Leaf)) {
        throw "Missing signing file: $signFile"
    }
}
$signKeytool = $null
if ($env:JAVA_HOME) {
    $signJavaKeytool = Join-Path $env:JAVA_HOME 'bin\keytool.exe'
    if (Test-Path -LiteralPath $signJavaKeytool) { $signKeytool = $signJavaKeytool }
}
if (-not $signKeytool) {
    $signKeytoolCommand = Get-Command keytool -ErrorAction SilentlyContinue
    if ($signKeytoolCommand) { $signKeytool = $signKeytoolCommand.Source }
}
if (-not $signKeytool -and (Test-Path -LiteralPath 'C:\Program Files\Java\jdk-23\bin\keytool.exe')) {
    $signKeytool = 'C:\Program Files\Java\jdk-23\bin\keytool.exe'
}
if (-not $signKeytool) { throw 'A JDK with keytool is required to verify the original signing certificate.' }
New-Item -ItemType Directory -Path $SbScratch -Force | Out-Null
$signCertificate = Join-Path $SbScratch 'signing-certificate.der'
. (Join-Path $PSScriptRoot 'Native-Command.ps1')
$signKeytoolResult = Invoke-CapturedNative -FilePath $signKeytool -Arguments @('-exportcert', '-storetype', 'PKCS12', '-keystore', $signKeystore, '-storepass:file', $signPasswordFile, '-alias', 'sentrybridge', '-file', $signCertificate)
if ($signKeytoolResult.ExitCode -ne 0) { throw 'Could not open the original signing keystore with its password and sentrybridge alias.' }
$signFingerprint = (Get-FileHash -LiteralPath $signCertificate -Algorithm SHA256).Hash.ToLowerInvariant()
$signExpected = '1afa17aec207256b2352089f9d4e0a1417d09c859e11fa349aef8d45d0a019c5'
if ($signFingerprint -ne $signExpected) {
    throw "Signing certificate differs from the previously configured release key. Observed SHA-256: $signFingerprint"
}
$signPassword = [System.IO.File]::ReadAllText($signPasswordFile).TrimEnd([char[]]@([char]13, [char]10))
if ([string]::IsNullOrWhiteSpace($signPassword)) { throw 'The signing password file is empty.' }
$env:SB_KEYSTORE_FILE = $signKeystore
$env:SB_KEYSTORE_PASSWORD = $signPassword
$env:SB_KEY_PASSWORD = $signPassword
$env:SB_KEY_ALIAS = 'sentrybridge'
$env:SB_REQUIRE_RELEASE_SIGNING = 'true'
$signPassword = $null
Write-Host 'Original signing certificate verified. Local release signing is configured for this PowerShell session.'
