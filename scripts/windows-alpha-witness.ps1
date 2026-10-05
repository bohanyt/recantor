param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedSha,

    [ValidateSet('Preflight', 'PostRun', 'All')]
    [string]$Phase = 'All',

    [string]$EvidenceDir = (Join-Path ([System.IO.Path]::GetTempPath()) 'recantor-alpha-evidence'),

    [string]$LiveSessionId,
    [string]$UploadSessionId,
    [string]$UploadToken
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$compose = @('docker', 'compose', '--env-file', '.env', '-f', 'infra/compose.yaml')

function Invoke-Checked {
    param([Parameter(Mandatory = $true)][string[]]$Command)
    $exe = $Command[0]
    $arguments = @($Command | Select-Object -Skip 1)
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE."
    }
}

function Get-Text {
    param([Parameter(Mandatory = $true)][string[]]$Command)
    $exe = $Command[0]
    $arguments = @($Command | Select-Object -Skip 1)
    $text = (& $exe @arguments 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE."
    }
    return $text
}

function Get-GroqSecret {
    if (-not [string]::IsNullOrWhiteSpace($env:GROQ_API_KEY)) {
        return $env:GROQ_API_KEY
    }
    if (Test-Path '.env') {
        foreach ($line in Get-Content '.env') {
            if ($line -match '^\s*GROQ_API_KEY\s*=\s*(.*)\s*$') {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return ''
}

function Assert-CleanSource {
    $actualSha = (Get-Text @('git', 'rev-parse', 'HEAD')).Trim().ToLowerInvariant()
    if ($actualSha -ne $ExpectedSha.ToLowerInvariant()) {
        throw 'Git HEAD does not match the approved exact SHA.'
    }

    $status = Get-Text @('git', 'status', '--porcelain', '--untracked-files=all')
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw 'Git worktree is not clean.'
    }
    return $actualSha
}

function Invoke-ApiJson {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [hashtable]$Headers = @{}
    )
    return Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Get -TimeoutSec 15
}

$repoRoot = (Get-Location).Path
$actualSha = Assert-CleanSource

if (-not (Test-Path '.env')) {
    throw '.env is required for the final Windows witness.'
}

Invoke-Checked @('docker', 'version')
Invoke-Checked @('docker', 'compose', 'version')
Invoke-Checked ($compose + @('config', '--quiet'))

$groqSecret = Get-GroqSecret
if ([string]::IsNullOrWhiteSpace($groqSecret)) {
    throw 'GROQ_API_KEY is not configured for the final Windows witness.'
}

New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
$resolvedEvidenceDir = (Resolve-Path $EvidenceDir).Path
if ($resolvedEvidenceDir.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'EvidenceDir must be outside the repository so evidence cannot dirty the worktree.'
}

$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/healthz' -Method Get -TimeoutSec 15
$ready = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/readyz' -Method Get -TimeoutSec 15
$web = Invoke-WebRequest -Uri 'http://127.0.0.1:5173' -Method Get -TimeoutSec 15
if ($web.StatusCode -ne 200) {
    throw 'Web shell is not healthy.'
}

$psText = Get-Text ($compose + @('ps'))
$psText | Set-Content -Path (Join-Path $resolvedEvidenceDir 'compose-ps.txt') -Encoding UTF8

if ($Phase -in @('PostRun', 'All')) {
    if ([string]::IsNullOrWhiteSpace($LiveSessionId)) {
        throw 'LiveSessionId is required for PostRun.'
    }
    if ([string]::IsNullOrWhiteSpace($UploadSessionId) -or [string]::IsNullOrWhiteSpace($UploadToken)) {
        throw 'UploadSessionId and UploadToken are required for PostRun.'
    }

    $liveState = Invoke-ApiJson "http://127.0.0.1:8000/api/v1/sessions/$LiveSessionId/recording-state"
    $liveTranscript = Invoke-ApiJson "http://127.0.0.1:8000/api/v1/sessions/$LiveSessionId/transcript?after_sequence=0&limit=100"
    if ($liveState.accepted_count -lt 1) {
        throw 'Live post-run assertion failed: no durable archive chunk.'
    }
    if (@($liveTranscript.segments).Count -lt 1) {
        throw 'Live post-run assertion failed: no canonical transcript segment.'
    }

    $uploadHeaders = @{ 'X-Recantor-Upload-Token' = $UploadToken }
    $uploadResult = Invoke-ApiJson "http://127.0.0.1:8000/api/v1/uploads/$UploadSessionId/result" $uploadHeaders
    $uploadTranscript = Invoke-ApiJson "http://127.0.0.1:8000/api/v1/uploads/$UploadSessionId/transcript?limit=100" $uploadHeaders
    if ($uploadResult.state -ne 'complete' -or -not $uploadResult.exports_available) {
        throw 'Upload post-run assertion failed: result is not complete/exportable.'
    }
    if (@($uploadTranscript.segments).Count -lt 1) {
        throw 'Upload post-run assertion failed: no canonical transcript segment.'
    }

    foreach ($format in @('txt', 'json', 'vtt', 'srt')) {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/uploads/$UploadSessionId/exports/$format" -Headers $uploadHeaders -Method Get -TimeoutSec 30
        if ($response.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($response.Content)) {
            throw "Upload export assertion failed for $format."
        }
        $response.Content | Set-Content -Path (Join-Path $resolvedEvidenceDir "upload-export.$format") -Encoding UTF8
    }

    $liveState | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $resolvedEvidenceDir 'live-state.json') -Encoding UTF8
    $liveTranscript | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $resolvedEvidenceDir 'live-transcript.json') -Encoding UTF8
    $uploadResult | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $resolvedEvidenceDir 'upload-result.json') -Encoding UTF8
    $uploadTranscript | ConvertTo-Json -Depth 8 | Set-Content -Path (Join-Path $resolvedEvidenceDir 'upload-transcript.json') -Encoding UTF8
}

$logs = Get-Text ($compose + @('logs', '--no-color'))
if ($logs.Contains($groqSecret)) {
    throw 'Secret-safety assertion failed: configured Groq secret appears in Compose logs.'
}
$logs.Replace($groqSecret, '[REDACTED]') |
    Set-Content -Path (Join-Path $resolvedEvidenceDir 'compose.log') -Encoding UTF8

$diff = (Get-Text @('git', 'diff', '--no-ext-diff', 'HEAD')) + (Get-Text @('git', 'diff', '--cached', '--no-ext-diff', 'HEAD'))
if ($diff.Contains($groqSecret)) {
    throw 'Secret-safety assertion failed: configured Groq secret appears in tracked diff.'
}
if (-not [string]::IsNullOrWhiteSpace($diff)) {
    throw 'Post-run source assertion failed: tracked diff is not empty.'
}

$finalSha = Assert-CleanSource
$summary = [ordered]@{
    expected_sha = $ExpectedSha.ToLowerInvariant()
    actual_sha = $finalSha
    phase = $Phase
    docker_available = $true
    compose_available = $true
    api_health = $true
    api_ready = $true
    web_healthy = $true
    source_clean = $true
    secret_absent_from_logs = $true
    secret_absent_from_tracked_diff = $true
}
$summary | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $resolvedEvidenceDir 'summary.json') -Encoding UTF8

Write-Host "WINDOWS_ALPHA_WITNESS_HELPER_PASS phase=$Phase"
