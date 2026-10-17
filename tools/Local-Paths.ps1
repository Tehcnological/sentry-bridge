# Dot-source to locate the ignored local tool and signing folders. They default to this clone;
# SB_LOCAL_TOOLS and SB_SIGNING_DIR can point at the folders of another configured clone, so
# a second checkout reuses them and never copies the signing key.
# Builds only read those folders; temporary files and caches go to $SbScratch in this checkout.
$SbRepoRoot = Split-Path -Parent $PSScriptRoot
$SbLocalTools = if ($env:SB_LOCAL_TOOLS) { $env:SB_LOCAL_TOOLS } else { Join-Path $SbRepoRoot '.local-tools' }
$SbSigningDir = if ($env:SB_SIGNING_DIR) { $env:SB_SIGNING_DIR } else { Join-Path $SbRepoRoot 'local-signing' }
$SbScratch = Join-Path (Join-Path $SbRepoRoot 'build') 'local'
