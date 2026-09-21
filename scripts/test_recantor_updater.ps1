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


$backupCandidate = New-TestManifest -Version "v0.1.0-alpha.6" -BackupRequired $true
$backupState = New-ReleaseState
$backupState.current = $v1
Assert-Throws {
    Apply-Release -Mode update -State $backupState -Candidate $backupCandidate -SelectedChannel alpha -StatePath (Join-Path $tmp "backup-state.json") -ActiveEnvPath (Join-Path $tmp "backup-active.env") -PendingEnvPath (Join-Path $tmp "backup-pending.env")
} "external recovery checkpoint" "backup-required candidate is gated before apply"

$env:RECANTOR_UPDATER_TEST_MODE = "1"
$env:RECANTOR_UPDATER_TEST_INTERRUPT_AFTER_PHASE = "pulling"
Assert-Throws { Invoke-TestInterruption -Phase "pulling" } "TEST_INTERRUPTION_AFTER_pulling" "gated interruption hook"
Remove-Item Env:RECANTOR_UPDATER_TEST_INTERRUPT_AFTER_PHASE -ErrorAction SilentlyContinue
Remove-Item Env:RECANTOR_UPDATER_TEST_MODE -ErrorAction SilentlyContinue

# Focused fail-closed rollback-attempt regression with Docker boundaries stubbed.
function Assert-LocalPreflight { param([string]$ImageEnv) }
function Invoke-Docker { param([string[]]$Arguments) }
function Start-ReleaseInfrastructure { param([string]$ImageEnv) }
function Invoke-CandidateMigration { param([string]$ImageEnv) }
function Start-ApplicationServices { param([string]$ImageEnv,[string]$OverrideFile) }
$script:RollbackHealthCalls = 0
function Wait-ReleaseHealthy {
    $script:RollbackHealthCalls += 1
    throw "synthetic health failure $script:RollbackHealthCalls"
}

$rollbackFailState = New-ReleaseState
$rollbackFailState.channel = "alpha"
$rollbackFailState.current = $v1
$rollbackFailCandidate = New-TestManifest -Version "v0.1.0-alpha.3" -SafeTo @("v0.1.0-alpha.2")
$rollbackFailStatePath = Join-Path $tmp "rollback-fail-state.json"
Assert-Throws {
    Apply-Release -Mode update -State $rollbackFailState -Candidate $rollbackFailCandidate -SelectedChannel alpha -StatePath $rollbackFailStatePath -ActiveEnvPath (Join-Path $tmp "rollback-fail-active.env") -PendingEnvPath (Join-Path $tmp "rollback-fail-pending.env")
} "synthetic health failure" "rollback attempt health failure is surfaced"
$rollbackFailPersisted = Read-ReleaseState $rollbackFailStatePath
Assert-Equal $rollbackFailPersisted.pending.phase "manual_recovery_required" "failed rollback enters manual recovery"
Assert-Equal $rollbackFailPersisted.last_attempt.result "rollback_failed_manual_recovery" "failed rollback result is truthful"
Assert-Equal $rollbackFailPersisted.current.version "v0.1.0-alpha.2" "failed rollback never advances current identity"

# Manual rollback target health failure must leave current state truthful and restore active env.
$manualState = New-ReleaseState
$manualState.current = $v2
$manualState.previous = $v1
$manualStatePath = Join-Path $tmp "manual-rollback-state.json"
$manualEnvPath = Join-Path $tmp "manual-rollback-active.env"
Write-AtomicJson -Path $manualStatePath -Object $manualState
function Wait-ReleaseHealthy { throw "synthetic manual rollback target health failure" }
Assert-Throws {
    Invoke-ManualRollback -State $manualState -StatePath $manualStatePath -ActiveEnvPath $manualEnvPath
} "active image state was restored" "manual rollback health failure restores truthful image state"
Assert-Equal $manualState.current.version "v0.1.0-alpha.3" "manual rollback failure leaves current unchanged"
$manualEnvText = Get-Content -LiteralPath $manualEnvPath -Raw
Assert-True ($manualEnvText -match "RECANTOR_RELEASE_VERSION=v0.1.0-alpha.3") "manual rollback failure restores current active env"

$source = Get-Content -LiteralPath (Join-Path $PSScriptRoot "recantor.ps1") -Raw
Assert-True ($source -notmatch 'alembic\s+downgrade') "updater contains no automatic Alembic downgrade"
Assert-True ($source -notmatch 'down\s+-v') "updater contains no volume-deleting recovery path"

Write-Host "RECANTOR_UPDATER_UNIT_PASS"
