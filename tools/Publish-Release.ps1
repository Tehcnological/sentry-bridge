# Requires GitHub CLI authentication in a normal terminal; does not rebuild or run Actions.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Tag,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$Commit,
    [Parameter(Mandatory=$true)][string]$NotesFile,
    [string]$Repository = 'Tehcnological/sentry-bridge',
    # Releases normally come from master. Set this only for a deliberate release from another branch.
    [switch]$AllowUnmergedCommit,
    # Marks a test release as a pre-release, so it never becomes Latest. Tags with a suffix always are.
    [switch]$Prerelease
)
$ErrorActionPreference = 'Stop'
# GitHub reports commit SHAs in lowercase; send and compare the same form.
$Commit = $Commit.ToLowerInvariant()
$publishRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$publishPython = (Get-Content -LiteralPath (Join-Path $SbLocalTools 'windows-environment.json') -Raw | ConvertFrom-Json).python
$publishBundle = Join-Path $publishRoot "dist/release/$Tag"
if ($Tag -notmatch '^v\d+\.\d+\.\d+(-[A-Za-z0-9.-]+)?$') { throw 'Invalid version tag.' }
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Invalid GitHub repository name.' }
if (-not (Test-Path -LiteralPath $NotesFile -PathType Leaf)) { throw 'Release notes file is required.' }
$publishGh = (Get-Command gh -ErrorAction Stop).Source
. (Join-Path $PSScriptRoot 'Native-Command.ps1')
& $publishPython (Join-Path $PSScriptRoot 'release_bundle.py') verify $publishBundle --tag $Tag
if ($LASTEXITCODE -ne 0) { throw 'Release bundle failed verification.' }
# GitHub's releases/tags/<tag> lookup never returns drafts, so releases are found in the list,
# which includes drafts for accounts with push access. Every page is read before deciding.
function Get-PublishReleases {
    $matching = @()
    for ($page = 1; ; $page++) {
        if ($page -gt 100) { throw 'Too many releases to check; inspect the releases in GitHub.' }
        $listing = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', '-X', 'GET', "repos/$Repository/releases", '-f', 'per_page=100', '-f', "page=$page")
        if ($listing.ExitCode -ne 0) { throw 'Could not list releases; verify GitHub authentication and repository access.' }
        # Assign first: Windows PowerShell 5.1 passes a JSON array down the pipeline as one object.
        $releases = ($listing.Stdout -join [Environment]::NewLine) | ConvertFrom-Json
        $releases = @($releases | Where-Object { $null -ne $_ })
        $matching += @($releases | Where-Object { $_.tag_name -eq $Tag })
        if ($releases.Count -lt 100) { return $matching }
    }
}
# The commit the tag points to (following annotated tags), or $null when the tag does not exist.
# A tag GitHub has just created can take a moment to appear; -Attempts waits for it.
function Get-PublishTagCommit([int]$Attempts = 1) {
    for ($attempt = 1; ; $attempt++) {
        $tagRef = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/git/ref/tags/$Tag")
        if ($tagRef.ExitCode -eq 0) { break }
        if (($tagRef.Output -join ' ') -notmatch 'HTTP 404') { throw 'Could not check the tag.' }
        if ($attempt -ge $Attempts) { return $null }
        Start-Sleep -Seconds 2
    }
    $tagObject = (($tagRef.Stdout -join [Environment]::NewLine) | ConvertFrom-Json).object
    for ($depth = 0; $tagObject.type -eq 'tag' -and $depth -lt 5; $depth++) {
        $annotation = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/git/tags/$($tagObject.sha)")
        if ($annotation.ExitCode -ne 0) { throw 'Could not resolve the annotated tag.' }
        $tagObject = (($annotation.Stdout -join [Environment]::NewLine) | ConvertFrom-Json).object
    }
    if ($tagObject.type -ne 'commit') { throw 'The tag does not point to a commit.' }
    return [string]$tagObject.sha
}
if ((Get-PublishReleases).Count -ne 0) { throw 'A release or draft for that tag already exists; this script never replaces existing downloads.' }
$publishTree = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/git/trees/${Commit}?recursive=1")
if ($publishTree.ExitCode -ne 0) { throw 'Could not read the intended GitHub source commit.' }
# Scratch files go to this checkout's build\local: SB_LOCAL_TOOLS may be another clone's folder,
# which is only read.
New-Item -ItemType Directory -Path $SbScratch -Force | Out-Null
$publishTreePath = Join-Path $SbScratch 'release-source-tree.json'
$publishTree.Stdout | Set-Content -LiteralPath $publishTreePath -Encoding UTF8
& $publishPython (Join-Path $PSScriptRoot 'release_bundle.py') verify $publishBundle --tree-json $publishTreePath --tag $Tag
if ($LASTEXITCODE -ne 0) { throw 'Release binaries were built from different sources than the intended commit.' }
# After the tree read, which needs the same Git data access: a 404 now means that the tag does not
# exist, not that the token cannot see it.
$publishTagCommit = Get-PublishTagCommit
if ($null -ne $publishTagCommit -and $publishTagCommit -ne $Commit) { throw 'Existing tag points to a different commit.' }
if (-not $AllowUnmergedCommit) {
    # master must contain the commit (identical or ahead), so a release cannot ship unreviewed work.
    $publishCompare = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/compare/$Commit...master")
    if ($publishCompare.ExitCode -ne 0) { throw 'Could not compare the release commit with master.' }
    $publishStatus = (($publishCompare.Stdout -join [Environment]::NewLine) | ConvertFrom-Json).status
    if ($publishStatus -notin @('identical', 'ahead')) { throw 'The release commit is not on master. Merge it first, or pass -AllowUnmergedCommit.' }
}
# Require manual-only triggers in every workflow on master and the target commit:
# GitHub can consult the default branch when a new tag is created.
foreach ($publishRef in @('master', $Commit)) {
    $publishListing = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/contents/.github/workflows?ref=$publishRef")
    if ($publishListing.ExitCode -ne 0) {
        if (($publishListing.Output -join ' ') -match 'HTTP 404') { continue }  # no workflows at all
        throw 'Could not verify the Actions trigger settings.'
    }
    # Assign first: Windows PowerShell 5.1 passes a JSON array down the pipeline as one object.
    $publishListingItems = ($publishListing.Stdout -join [Environment]::NewLine) | ConvertFrom-Json
    $publishWorkflows = @($publishListingItems | Where-Object { $_.type -eq 'file' -and $_.name -match '\.ya?ml$' })
    if ($publishWorkflows.Count -eq 0) { throw 'Could not verify the Actions trigger settings.' }
    foreach ($publishEntry in $publishWorkflows) {
        $publishWorkflow = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/contents/.github/workflows/$($publishEntry.name)?ref=$publishRef")
        if ($publishWorkflow.ExitCode -ne 0) { throw 'Could not verify the Actions trigger settings.' }
        $publishWorkflowJson = ($publishWorkflow.Stdout -join [Environment]::NewLine) | ConvertFrom-Json
        $publishWorkflowText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($publishWorkflowJson.content))
        $publishWorkflowPath = Join-Path $SbScratch 'release-workflow.yml'
        $publishWorkflowText | Set-Content -LiteralPath $publishWorkflowPath -Encoding UTF8
        # No double quotes in the program: Windows PowerShell 5.1 strips them from native arguments.
        & $publishPython -c 'import sys,yaml; d=yaml.safe_load(open(sys.argv[1],encoding=''utf-8-sig'')); triggers=d.get(''on'',d.get(True)); sys.exit(0 if isinstance(triggers,dict) and set(triggers)=={''workflow_dispatch''} else 1)' $publishWorkflowPath
        if ($LASTEXITCODE -ne 0) { throw "Merge the manual-only Actions workflow before publishing a local release ($($publishEntry.name) has automatic triggers)." }
    }
}
$publishArguments = @('release','create',$Tag,'--repo',$Repository,'--target',$Commit,'--draft','--title',$Tag,'--notes-file',(Resolve-Path -LiteralPath $NotesFile).Path)
if ($Prerelease -or $Tag.Contains('-')) { $publishArguments += '--prerelease' }
# Keep in step with DISTRIBUTION_ASSETS in tools/release_bundle.py, plus SHA256SUMS and the manifest.
$publishFiles = @('sentry-bridge-gui.exe','sentry-ctl.exe','sentry-bridge.apk','sentry-bridge.ipa','sentry-bridge-source.zip','LICENSE','NOTICE.txt','SHA256SUMS','release-manifest.json')
$publishArguments += $publishFiles | ForEach-Object { Join-Path $publishBundle $_ }
& $publishGh @publishArguments
if ($LASTEXITCODE -ne 0) { throw 'Draft release upload failed. Inspect the draft before retrying; no published release was changed.' }
# A new draft can take a moment to appear in the release list.
for ($publishAttempt = 1; ; $publishAttempt++) {
    $publishDrafts = @(Get-PublishReleases | Where-Object { $_.draft -eq $true })
    if ($publishDrafts.Count -ne 0 -or $publishAttempt -ge 5) { break }
    Start-Sleep -Seconds 2
}
if ($publishDrafts.Count -ne 1) { throw 'Could not identify the uploaded draft by its tag; inspect the drafts in GitHub. Nothing was published.' }
$publishReleaseId = [int64]$publishDrafts[0].id
$publishDraft = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/releases/$publishReleaseId")
if ($publishDraft.ExitCode -ne 0) { throw 'Could not verify uploaded draft assets; the release remains a draft.' }
$publishDraftRelease = ($publishDraft.Stdout -join [Environment]::NewLine) | ConvertFrom-Json
if ($publishDraftRelease.tag_name -ne $Tag -or $publishDraftRelease.draft -ne $true) { throw 'Draft identity changed; the release remains a draft.' }
# Publishing creates the tag at the draft's target, so the target must be the verified commit.
if ($publishDraftRelease.target_commitish -ne $Commit) { throw 'Draft targets a different commit; the release remains a draft.' }
$publishUploaded = $publishDraftRelease.assets
$publishNames = @($publishUploaded | ForEach-Object { $_.name } | Sort-Object)
$publishExpected = $publishFiles | Sort-Object
if (($publishNames -join ',') -ne ($publishExpected -join ',')) { throw 'Draft is missing assets; it will not be published.' }
foreach ($publishAsset in $publishUploaded) {
    $publishLocalAsset = Join-Path $publishBundle $publishAsset.name
    if ($publishAsset.size -ne (Get-Item -LiteralPath $publishLocalAsset).Length) { throw 'Uploaded asset size differs; the release remains a draft.' }
    $publishExpectedDigest = 'sha256:' + (Get-FileHash -LiteralPath $publishLocalAsset -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($publishAsset.digest -ne $publishExpectedDigest) { throw 'Uploaded asset checksum differs or is unavailable; the release remains a draft.' }
}
# A tag created since the first check would be used as it is, whatever the draft's target.
$publishTagCommit = Get-PublishTagCommit
if ($null -ne $publishTagCommit -and $publishTagCommit -ne $Commit) { throw 'The tag now points to a different commit; the release remains a draft.' }
# Publish the verified draft by ID, so a different release with the same tag can never be published.
$publishResult = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', '-X', 'PATCH', "repos/$Repository/releases/$publishReleaseId", '-F', 'draft=false')
if ($publishResult.ExitCode -ne 0) { throw 'Publication failed; inspect the draft in GitHub.' }
$publishPublished = Invoke-CapturedNative -FilePath $publishGh -Arguments @('api', "repos/$Repository/releases/tags/$Tag")
if ($publishPublished.ExitCode -ne 0) { throw 'Publication could not be confirmed; inspect the release in GitHub.' }
$publishPublishedRelease = ($publishPublished.Stdout -join [Environment]::NewLine) | ConvertFrom-Json
if ([int64]$publishPublishedRelease.id -ne $publishReleaseId) { throw 'Publication could not be confirmed; inspect the release in GitHub.' }
if ([bool]$publishPublishedRelease.prerelease -ne ($Prerelease -or $Tag.Contains('-'))) { throw 'Published, but the pre-release flag is wrong; fix it in GitHub now (it decides which release the update check offers).' }
$publishTagCommit = Get-PublishTagCommit -Attempts 5
if ($null -eq $publishTagCommit) { throw "Published, but the tag is not visible yet; check in GitHub that $Tag points to $Commit." }
if ($publishTagCommit -ne $Commit) { throw 'The published tag does not point to the release commit; inspect the release in GitHub now.' }
Write-Host "Published https://github.com/$Repository/releases/tag/$Tag"
