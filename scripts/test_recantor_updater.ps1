$ErrorActionPreference = "Stop"
$env:RECANTOR_UPDATER_LIBRARY_MODE = "1"
. (Join-Path $PSScriptRoot "recantor.ps1")

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "ASSERT TRUE FAILED: $Message"
    }
}

function Assert-Equal {
    param($Actual, $Expected, [string]$Message)
    if ($Actual -ne $Expected) {
        throw "ASSERT EQUAL FAILED: $Message. Expected '$Expected', got '$Actual'."
    }
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern, [string]$Message)
    $threw = $false
    try {
        & $Action
    }
    catch {
        $threw = $true
        if ($Pattern -and $_.Exception.Message -notmatch $Pattern) {
            throw "ASSERT THROWS FAILED: $Message. Wrong message: $($_.Exception.Message)"
        }
    }
    if (-not $threw) {
        throw "ASSERT THROWS FAILED: $Message"
    }
}

function New-TestManifest {
    param(
        [int]$Format = 2,
        [string]$Version = "v0.1.0-alpha.3",
        [string]$Channel = "alpha",
        [string[]]$SafeTo = @("v0.1.0-alpha.2"),
        [bool]$BackupRequired = $false
    )

    $compat = [pscustomobject]@{
        platforms = @("linux/amd64")
        schema_head = "0009"
        automatic_database_downgrade = $false
    }

    if ($Format -eq 2) {
        Add-Member -InputObject $compat -NotePropertyName application_rollback_safe_to_versions -NotePropertyValue $SafeTo
        Add-Member -InputObject $compat -NotePropertyName backup_required -NotePropertyValue $BackupRequired
    }

    return [pscustomobject]@{
        format_version = $Format
        version = $Version
        channel = $Channel
        source = [pscustomobject]@{
            repository = "https://github.com/bohanyt/recantor"
            revision = ("a" * 40)
        }
        images = [pscustomobject]@{
            api = [pscustomobject]@{
                repository = "ghcr.io/bohanyt/recantor-api"
                digest = ("sha256:" + ("b" * 64))
                reference = ("ghcr.io/bohanyt/recantor-api@sha256:" + ("b" * 64))
            }
            web = [pscustomobject]@{
                repository = "ghcr.io/bohanyt/recantor-web"
                digest = ("sha256:" + ("c" * 64))
                reference = ("ghcr.io/bohanyt/recantor-web@sha256:" + ("c" * 64))
            }
        }
        compatibility = $compat
        compose_file = "infra/compose.release.yaml"
    }
}

Assert-Equal (Get-RecantorChannelForVersion "v1.2.3") "stable" "stable version"
Assert-Equal (Get-RecantorChannelForVersion "v1.2.3-alpha.1") "alpha" "alpha version"
Assert-Equal (Get-RecantorChannelForVersion "v1.2.3-beta.1") "beta" "beta version"
Assert-Equal (Get-RecantorChannelForVersion "v1.2.3-rc.1") "beta" "rc maps to beta"
Assert-Throws { Get-RecantorChannelForVersion "v1.2.3-preview.1" } "Unsupported" "unknown prerelease"

Assert-Equal (Compare-RecantorVersion "v1.2.3" "v1.2.2") 1 "newer stable compares greater"
Assert-Equal (Compare-RecantorVersion "v1.2.3-alpha.2" "v1.2.3-alpha.1") 1 "newer alpha ordinal compares greater"
Assert-Equal (Compare-RecantorVersion "v1.2.3" "v1.2.3-rc.1") 1 "stable outranks prerelease"
Assert-Equal (Compare-RecantorVersion "v1.2.3-alpha.1" "v1.2.3-alpha.1") 0 "same version compares equal"

$v1 = New-TestManifest -Format 1 -Version "v0.1.0-alpha.2"
Assert-ReleaseManifest $v1
Assert-True (-not (Test-ApplicationRollbackSafe -CandidateManifest $v1 -PreviousVersion "v0.1.0-alpha.1")) "format 1 has no inferred rollback safety"

$v2 = New-TestManifest
Assert-ReleaseManifest $v2
Assert-True (Test-ApplicationRollbackSafe -CandidateManifest $v2 -PreviousVersion "v0.1.0-alpha.2") "explicit safe rollback"
Assert-True (-not (Test-ApplicationRollbackSafe -CandidateManifest $v2 -PreviousVersion "v0.1.0-alpha.1")) "non-listed rollback is unsafe"

$badRef = New-TestManifest
$badRef.images.api.reference = "ghcr.io/bohanyt/recantor-api:latest"
Assert-Throws { Assert-ReleaseManifest $badRef } "immutable" "mutable image tags rejected"

$badChannel = New-TestManifest
$badChannel.channel = "stable"
Assert-Throws { Assert-ReleaseManifest $badChannel } "requires channel" "channel mismatch rejected"

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("recantor-updater-test-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    $statePath = Join-Path $tmp "state.json"
    $envPath = Join-Path $tmp "active-images.env"

    $state = New-ReleaseState
    $state.channel = "alpha"
    $state.current = $v1
    Write-AtomicJson -Path $statePath -Object $state

    $loaded = Read-ReleaseState $statePath
    Assert-Equal $loaded.channel "alpha" "state channel persists"
    Assert-Equal $loaded.current.version "v0.1.0-alpha.2" "current known-good persists"

    Write-ImageEnv -Path $envPath -Manifest $v1
    $envText = Get-Content -LiteralPath $envPath -Raw
    Assert-True ($envText -match "RECANTOR_API_IMAGE=ghcr.io/bohanyt/recantor-api@sha256:") "active env stores exact API digest"
    Assert-True ($envText -match "RECANTOR_WEB_IMAGE=ghcr.io/bohanyt/recantor-web@sha256:") "active env stores exact Web digest"
    Assert-True ($envText -notmatch "GROQ") "active image env stores no provider secret"

    $state.pending = [pscustomobject]@{ candidate_version = "v0.1.0-alpha.3" }
    Assert-PendingCompatible -State $state -Candidate $v2

    $different = New-TestManifest -Version "v0.1.0-alpha.4"
    Assert-Throws { Assert-PendingCompatible -State $state -Candidate $different } "different update is pending" "pending identity fences another candidate"
}
finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$source = Get-Content -LiteralPath (Join-Path $PSScriptRoot "recantor.ps1") -Raw
Assert-True ($source -notmatch 'alembic\s+downgrade') "updater contains no automatic Alembic downgrade"
Assert-True ($source -notmatch 'down\s+-v') "updater contains no volume-deleting recovery path"

Write-Host "RECANTOR_UPDATER_UNIT_PASS"
