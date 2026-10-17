# Capture native diagnostics without Windows PowerShell 5.1 treating stderr as
# a terminating PowerShell error. Callers must check ExitCode before using Output.
function Invoke-CapturedNative {
    param([string]$FilePath, [string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        throw "Native command is missing: $FilePath"
    }
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    $nativeOutput = @(& $FilePath @Arguments 2>&1)
    $nativeExitCode = $LASTEXITCODE
    # Output keeps stdout and stderr together for messages; parse JSON from Stdout only, so a
    # notice on stderr cannot corrupt it.
    [pscustomobject]@{
        ExitCode = $nativeExitCode
        Output = @($nativeOutput | ForEach-Object { $_.ToString() })
        Stdout = @($nativeOutput | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() })
    }
}
