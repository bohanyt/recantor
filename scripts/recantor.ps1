param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "update", "status", "rollback")]
    [string]$Command = "status",
    [string]$ManifestPath,
    [string]$ManifestUri,
    [ValidateSet("stable", "beta", "alpha")]
    [string]$Channel,
    [string]$StateDir = ".recantor",
    [string]$ComposeFile = "infra/compose.release.yaml",
    [string]$EnvFile = ".env",
    [string]$RecoveryCheckpoint,
    [ValidateRange(5, 600)]
    [int]$HealthTimeoutSeconds = 120,
    [string]$TestCandidateComposeOverride
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$script:ApiRepository = "ghcr.io/bohanyt/recantor-api"
$script:WebRepository = "ghcr.io/bohanyt/recantor-web"
$script:AppServices = @("api", "stt-worker", "stt-upload-worker", "stt-reconciler", "media-worker", "media-reconciler", "tusd", "web")

function Get-RecantorChannelForVersion {
    param([string]$Version)
    if ($Version -match '^v\d+\.\d+\.\d+$') { return "stable" }
    if ($Version -match '^v\d+\.\d+\.\d+-alpha(?:\.[0-9A-Za-z-]+)*$') { return "alpha" }
    if ($Version -match '^v\d+\.\d+\.\d+-(?:beta|rc)(?:\.[0-9A-Za-z-]+)*$') { return "beta" }
    throw "Unsupported release version '$Version'."
}

function Test-HasProperty {
    param($Object, [string]$Name)
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

function Assert-ImmutableImageReference {
    param([string]$Reference, [string]$Repository)
    $escaped = [regex]::Escape($Repository)
    if ($Reference -notmatch "^$escaped@sha256:[0-9a-f]{64}$") {
        throw "Image reference must be immutable $Repository@sha256:<64 lowercase hex>."
    }
}

function Assert-ReleaseManifest {
    param($Manifest)
    if (-not (Test-HasProperty $Manifest "format_version")) { throw "Manifest is missing format_version." }
    $format = [int]$Manifest.format_version
    if ($format -ne 1 -and $format -ne 2) { throw "Manifest format_version must be 1 or 2." }

    $version = [string]$Manifest.version
    $expected = Get-RecantorChannelForVersion $version
    if ([string]$Manifest.channel -ne $expected) { throw "Manifest version '$version' requires channel '$expected'." }

    if ([string]$Manifest.source.repository -ne "https://github.com/bohanyt/recantor") { throw "Manifest source repository is not Recantor." }
    if ([string]$Manifest.source.revision -notmatch '^[0-9a-f]{40}$') { throw "Manifest source revision must be an exact Git SHA." }

    Assert-ImmutableImageReference ([string]$Manifest.images.api.reference) $script:ApiRepository
    Assert-ImmutableImageReference ([string]$Manifest.images.web.reference) $script:WebRepository

    if ([string]$Manifest.compatibility.schema_head -eq "") { throw "Manifest schema_head must not be empty." }
    if ([bool]$Manifest.compatibility.automatic_database_downgrade) { throw "Recantor updater never accepts automatic database downgrade." }

    if ($format -eq 2) {
        if (-not (Test-HasProperty $Manifest.compatibility "application_rollback_safe_to_versions")) { throw "Format 2 manifest requires application_rollback_safe_to_versions." }
        foreach ($v in @($Manifest.compatibility.application_rollback_safe_to_versions)) { [void](Get-RecantorChannelForVersion ([string]$v)) }
        if (-not (Test-HasProperty $Manifest.compatibility "backup_required")) { throw "Format 2 manifest requires backup_required." }
        if ($Manifest.compatibility.backup_required -isnot [bool]) { throw "Manifest backup_required must be boolean." }
    }
}

function Test-ApplicationRollbackSafe {
    param($CandidateManifest, [string]$PreviousVersion)
    if ([int]$CandidateManifest.format_version -ne 2) { return $false }
    foreach ($v in @($CandidateManifest.compatibility.application_rollback_safe_to_versions)) {
        if ([string]$v -eq $PreviousVersion) { return $true }
    }
    return $false
}

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "File not found: $Path" }
    return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
}

function Write-AtomicText {
    param([string]$Path, [string]$Content)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$Path.tmp.$PID"
    [IO.File]::WriteAllText($tmp, $Content, [Text.UTF8Encoding]::new($false))
    try {
        if (Test-Path -LiteralPath $Path) {
            try { [IO.File]::Replace($tmp, $Path, $null, $true) }
            catch { Move-Item -LiteralPath $tmp -Destination $Path -Force }
        } else {
            Move-Item -LiteralPath $tmp -Destination $Path
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
}

function Write-AtomicJson {
    param([string]$Path, $Object)
    Write-AtomicText $Path (($Object | ConvertTo-Json -Depth 40) + [Environment]::NewLine)
}

function New-ReleaseState {
    return [pscustomobject]@{
        format_version = 1
        channel = $null
        current = $null
        previous = $null
        pending = $null
        last_attempt = $null
    }
}

function Read-ReleaseState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return New-ReleaseState }
    $state = Read-JsonFile $Path
    foreach ($name in @("channel", "current", "previous", "pending", "last_attempt")) {
        if (-not (Test-HasProperty $state $name)) { Add-Member -InputObject $state -NotePropertyName $name -NotePropertyValue $null }
    }
    return $state
}

function Write-ImageEnv {
    param([string]$Path, $Manifest)
    $content = @(
        "RECANTOR_RELEASE_VERSION=$([string]$Manifest.version)",
        "RECANTOR_API_IMAGE=$([string]$Manifest.images.api.reference)",
        "RECANTOR_WEB_IMAGE=$([string]$Manifest.images.web.reference)"
    ) -join [Environment]::NewLine
    Write-AtomicText $Path ($content + [Environment]::NewLine)
}

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Get-ComposePrefix {
    param([string]$ImageEnv, [string]$OverrideFile)
    $args = @("compose", "--env-file", $EnvFile, "--env-file", $ImageEnv, "-f", $ComposeFile)
    if ($OverrideFile) { $args += @("-f", $OverrideFile) }
    return $args
}

function Invoke-Compose {
    param([string]$ImageEnv, [string[]]$Arguments, [string]$OverrideFile)
    $all = @(Get-ComposePrefix -ImageEnv $ImageEnv -OverrideFile $OverrideFile)
    $all += $Arguments
    Invoke-Docker $all
}

function Assert-LocalPreflight {
    param([string]$ImageEnv)
    if (-not (Test-Path -LiteralPath $ComposeFile)) { throw "Release Compose file not found: $ComposeFile" }
    if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Local environment file not found: $EnvFile" }
    if ($TestCandidateComposeOverride) {
        if ($env:RECANTOR_UPDATER_TEST_MODE -ne "1") { throw "TestCandidateComposeOverride is disabled outside updater test mode." }
        if (-not (Test-Path -LiteralPath $TestCandidateComposeOverride)) { throw "Test candidate Compose override not found." }
    }
    Invoke-Docker @("version")
    Invoke-Docker @("compose", "version")
    Invoke-Compose -ImageEnv $ImageEnv -Arguments @("config", "--quiet")
}

function Test-HttpEndpoint {
    param([string]$Uri)
    try {
        $params = @{ Uri = $Uri; TimeoutSec = 4; ErrorAction = "Stop" }
        if ($PSVersionTable.PSVersion.Major -lt 6) { $params["UseBasicParsing"] = $true }
        Invoke-WebRequest @params | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-ReleaseHealthy {
    $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((Test-HttpEndpoint "http://127.0.0.1:8000/healthz") -and (Test-HttpEndpoint "http://127.0.0.1:8000/readyz") -and (Test-HttpEndpoint "http://127.0.0.1:5173/")) { return }
        Start-Sleep -Seconds 2
    }
    throw "Release failed health/readiness/Web verification within $HealthTimeoutSeconds seconds."
}

function Start-ReleaseInfrastructure {
    param([string]$ImageEnv)
    Invoke-Compose -ImageEnv $ImageEnv -Arguments @("up", "-d", "postgres", "redis", "upload-storage-init")
}

function Invoke-CandidateMigration {
    param([string]$ImageEnv)
    Invoke-Compose -ImageEnv $ImageEnv -Arguments @("run", "--rm", "migrate")
}

function Start-ApplicationServices {
    param([string]$ImageEnv, [string]$OverrideFile)
    $args = @("up", "-d", "--no-deps") + $script:AppServices
    Invoke-Compose -ImageEnv $ImageEnv -Arguments $args -OverrideFile $OverrideFile
}

function Resolve-LatestManifestUri {
    param([string]$SelectedChannel)
    $headers = @{ "User-Agent" = "recantor-updater"; "Accept" = "application/vnd.github+json" }
    $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/bohanyt/recantor/releases?per_page=30" -Headers $headers -ErrorAction Stop
    foreach ($release in @($releases)) {
        if ($release.draft) { continue }
        try { $releaseChannel = Get-RecantorChannelForVersion ([string]$release.tag_name) }
        catch { continue }
        if ($releaseChannel -ne $SelectedChannel) { continue }
        $asset = @($release.assets) | Where-Object { $_.name -eq "release-manifest.json" } | Select-Object -First 1
        if ($null -ne $asset) { return [string]$asset.browser_download_url }
    }
    throw "No Recantor release manifest found for channel '$SelectedChannel'."
}

function Resolve-Manifest {
    param([string]$SelectedChannel)
    if ($ManifestPath -and $ManifestUri) { throw "Use either ManifestPath or ManifestUri, not both." }
    if ($ManifestPath) {
        $manifest = Read-JsonFile $ManifestPath
    } else {
        $uri = $ManifestUri
        if (-not $uri) { $uri = Resolve-LatestManifestUri $SelectedChannel }
        $manifest = Invoke-RestMethod -Uri $uri -Headers @{ "User-Agent" = "recantor-updater" } -ErrorAction Stop
    }
    Assert-ReleaseManifest $manifest
    if ([string]$manifest.channel -ne $SelectedChannel) { throw "Candidate channel '$($manifest.channel)' does not match selected channel '$SelectedChannel'." }
    return $manifest
}

function Assert-PendingCompatible {
    param($State, $Candidate)
    if ($null -eq $State.pending) { return }
    if ([string]$State.pending.candidate_version -ne [string]$Candidate.version) { throw "A different update is pending manual recovery: $($State.pending.candidate_version)." }
}

function Set-PendingState {
    param($State, $Candidate, [string]$Phase, [string]$Message)
    $State.pending = [pscustomobject]@{
        candidate_version = [string]$Candidate.version
        candidate = $Candidate
        phase = $Phase
        message = $Message
        recovery_checkpoint = $RecoveryCheckpoint
        updated_at_utc = [DateTime]::UtcNow.ToString("o")
    }
}

function Set-LastAttempt {
    param($State, [string]$CandidateVersion, [string]$Result, [string]$Message)
    $State.last_attempt = [pscustomobject]@{
        candidate_version = $CandidateVersion
        result = $Result
        message = $Message
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
    }
}

function Apply-Release {
    param([ValidateSet("install", "update")][string]$Mode, $State, $Candidate, [string]$SelectedChannel, [string]$StatePath, [string]$ActiveEnvPath, [string]$PendingEnvPath)

    if ($Mode -eq "install" -and $null -ne $State.current) { throw "Recantor is already installed according to updater state. Use update." }
    if ($Mode -eq "update" -and $null -eq $State.current) { throw "No installed known-good release is recorded. Use install first." }
    if ($Mode -eq "update" -and [int]$Candidate.format_version -ne 2) { throw "Updates require a format 2 manifest with explicit rollback/backup policy." }
    if ($null -ne $State.current -and [string]$State.current.version -eq [string]$Candidate.version) { throw "Release $($Candidate.version) is already current." }

    Assert-PendingCompatible -State $State -Candidate $Candidate

    if ([int]$Candidate.format_version -eq 2 -and [bool]$Candidate.compatibility.backup_required) {
        if (-not $RecoveryCheckpoint -or -not (Test-Path -LiteralPath $RecoveryCheckpoint)) { throw "Candidate requires an external recovery checkpoint before apply." }
    }

    Write-ImageEnv -Path $PendingEnvPath -Manifest $Candidate
    Assert-LocalPreflight -ImageEnv $PendingEnvPath
    Set-PendingState -State $State -Candidate $Candidate -Phase "pulling" -Message $null
    Write-AtomicJson -Path $StatePath -Object $State

    Invoke-Docker @("pull", [string]$Candidate.images.api.reference)
    Invoke-Docker @("pull", [string]$Candidate.images.web.reference)

    Set-PendingState -State $State -Candidate $Candidate -Phase "migrating" -Message $null
    Write-AtomicJson -Path $StatePath -Object $State
    Start-ReleaseInfrastructure -ImageEnv $PendingEnvPath

    try {
        Invoke-CandidateMigration -ImageEnv $PendingEnvPath
    } catch {
        Set-PendingState -State $State -Candidate $Candidate -Phase "manual_recovery_required" -Message "Candidate migration failed; automatic application rollback is not attempted."
        Set-LastAttempt -State $State -CandidateVersion ([string]$Candidate.version) -Result "migration_failed_manual_recovery" -Message $_.Exception.Message
        Write-AtomicJson -Path $StatePath -Object $State
        throw
    }

    Set-PendingState -State $State -Candidate $Candidate -Phase "activating" -Message $null
    Write-AtomicJson -Path $StatePath -Object $State

    try {
        Start-ApplicationServices -ImageEnv $PendingEnvPath -OverrideFile $TestCandidateComposeOverride
        Wait-ReleaseHealthy
    } catch {
        $activationError = $_.Exception.Message

        if ($null -eq $State.current) {
            Set-PendingState -State $State -Candidate $Candidate -Phase "manual_recovery_required" -Message "Initial install failed health verification and has no previous known-good release."
            Set-LastAttempt -State $State -CandidateVersion ([string]$Candidate.version) -Result "install_failed_manual_recovery" -Message $activationError
            Write-AtomicJson -Path $StatePath -Object $State
            throw
        }

        $previousVersion = [string]$State.current.version
        if (-not (Test-ApplicationRollbackSafe -CandidateManifest $Candidate -PreviousVersion $previousVersion)) {
            Set-PendingState -State $State -Candidate $Candidate -Phase "manual_recovery_required" -Message "Candidate failed and does not explicitly allow application rollback to $previousVersion."
            Set-LastAttempt -State $State -CandidateVersion ([string]$Candidate.version) -Result "rollback_refused_unsafe" -Message $activationError
            Write-AtomicJson -Path $StatePath -Object $State
            throw "Candidate failed; automatic rollback to $previousVersion is not declared schema-safe."
        }

        Write-ImageEnv -Path $ActiveEnvPath -Manifest $State.current
        Start-ApplicationServices -ImageEnv $ActiveEnvPath
        Wait-ReleaseHealthy
        $State.pending = $null
        Set-LastAttempt -State $State -CandidateVersion ([string]$Candidate.version) -Result "rolled_back_to_previous_known_good" -Message $activationError
        Write-AtomicJson -Path $StatePath -Object $State
        throw "Candidate activation failed and Recantor was rolled back to $previousVersion."
    }

    $oldCurrent = $State.current
    $State.previous = $oldCurrent
    $State.current = $Candidate
    $State.channel = $SelectedChannel
    $State.pending = $null
    Set-LastAttempt -State $State -CandidateVersion ([string]$Candidate.version) -Result "success" -Message $null
    Write-ImageEnv -Path $ActiveEnvPath -Manifest $Candidate
    Write-AtomicJson -Path $StatePath -Object $State
}

function Invoke-ManualRollback {
    param($State, [string]$StatePath, [string]$ActiveEnvPath)
    if ($null -eq $State.current -or $null -eq $State.previous) { throw "Manual rollback requires current and previous known-good releases." }
    $currentVersion = [string]$State.current.version
    $previousVersion = [string]$State.previous.version
    if (-not (Test-ApplicationRollbackSafe -CandidateManifest $State.current -PreviousVersion $previousVersion)) { throw "Current release does not explicitly declare application rollback to $previousVersion as safe." }

    $oldCurrent = $State.current
    $target = $State.previous
    Write-ImageEnv -Path $ActiveEnvPath -Manifest $target
    Start-ApplicationServices -ImageEnv $ActiveEnvPath
    Wait-ReleaseHealthy
    $State.current = $target
    $State.previous = $oldCurrent
    $State.pending = $null
    Set-LastAttempt -State $State -CandidateVersion $currentVersion -Result "manual_rollback_success" -Message "Activated $previousVersion without database downgrade."
    Write-AtomicJson -Path $StatePath -Object $State
}

function Invoke-RecantorUpdater {
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $statePath = Join-Path $StateDir "state.json"
    $activeEnvPath = Join-Path $StateDir "active-images.env"
    $pendingEnvPath = Join-Path $StateDir "pending-images.env"
    $state = Read-ReleaseState $statePath

    if ($Command -eq "status") { $state | ConvertTo-Json -Depth 40; return }
    if ($Command -eq "rollback") {
        Invoke-ManualRollback -State $state -StatePath $statePath -ActiveEnvPath $activeEnvPath
        Write-Host "Recantor rollback completed. Current: $($state.current.version)"
        return
    }

    $selectedChannel = $Channel
    if (-not $selectedChannel) {
        if ($state.channel) { $selectedChannel = [string]$state.channel }
        else { $selectedChannel = "stable" }
    }

    $candidate = Resolve-Manifest -SelectedChannel $selectedChannel
    Apply-Release -Mode $Command -State $state -Candidate $candidate -SelectedChannel $selectedChannel -StatePath $statePath -ActiveEnvPath $activeEnvPath -PendingEnvPath $pendingEnvPath
    Write-Host "Recantor $Command completed. Current: $($candidate.version)"
}

if ($env:RECANTOR_UPDATER_LIBRARY_MODE -ne "1") { Invoke-RecantorUpdater }
