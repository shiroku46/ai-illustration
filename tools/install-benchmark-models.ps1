[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ComfyUIRoot,

    [string]$ManifestPath = (Join-Path $PSScriptRoot "..\benchmark\model-install-manifest.v001.json"),

    [switch]$Execute,
    [switch]$AcknowledgeExactArtifacts,
    [switch]$AcknowledgeAnimaEvaluationOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ExpectedManifestSha256 = "1e7bc331054ee090a1570c8d44ac783d9a515acc5f9483bb4f2ad0c9c59dc183"

function Assert-SafeChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $candidateFull = [System.IO.Path]::GetFullPath($Candidate)
    $prefix = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escapes the ComfyUI root."
    }
    return $candidateFull
}

function Get-VerifiedFileState {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][long]$ExpectedSize,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $item = Get-Item -LiteralPath $Path
    $actualSha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($item.Length -ne $ExpectedSize -or $actualSha256 -ne $ExpectedSha256) {
        throw "Existing file does not match the exact manifest: $Path"
    }
    return [ordered]@{
        status = "present-verified"
        path = $Path
        size_bytes = [long]$item.Length
        sha256 = $actualSha256
    }
}

$manifestResolved = (Resolve-Path -LiteralPath $ManifestPath).Path
$rootResolved = (Resolve-Path -LiteralPath $ComfyUIRoot).Path
if (-not (Test-Path -LiteralPath $rootResolved -PathType Container)) {
    throw "ComfyUIRoot must be an existing directory."
}
$manifestSha256 = (Get-FileHash -LiteralPath $manifestResolved -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestSha256 -ne $ExpectedManifestSha256) {
    throw "Manifest bytes do not match the reviewed repository manifest."
}

$manifest = Get-Content -LiteralPath $manifestResolved -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.kind -ne "model-install-manifest" -or $manifest.schema_version -ne "1.0") {
    throw "Unsupported model installation manifest."
}
if ($manifest.id -ne "manzai-three-model-benchmark-install" -or $manifest.version -ne "v001") {
    throw "Unexpected model installation manifest identity."
}
if ($null -eq $manifest.models -or @($manifest.models).Count -lt 3) {
    throw "The manifest must contain at least three model entries."
}

if ($Execute) {
    if (-not $AcknowledgeExactArtifacts) {
        throw "-Execute requires -AcknowledgeExactArtifacts."
    }
    if (-not $AcknowledgeAnimaEvaluationOnly) {
        throw "-Execute requires -AcknowledgeAnimaEvaluationOnly."
    }
}

$results = [System.Collections.Generic.List[object]]::new()
foreach ($model in @($manifest.models)) {
    if ($model.benchmark_scope -notin @("production-candidate", "evaluation-only")) {
        throw "Unsupported benchmark scope for $($model.family)."
    }
    if ($model.family -eq "anima-aesthetic" -and $model.benchmark_scope -ne "evaluation-only") {
        throw "Anima must remain evaluation-only."
    }

    foreach ($artifact in @($model.artifacts)) {
        if ($artifact.required -ne $true) {
            throw "Optional artifacts are not accepted: $($artifact.id)"
        }
        $source = [System.Uri]$artifact.source_url
        if ($source.Scheme -ne "https") {
            throw "Artifact source must use HTTPS: $($artifact.id)"
        }
        $filename = [string]$artifact.filename
        if ([System.IO.Path]::GetFileName($filename) -ne $filename -or [string]::IsNullOrWhiteSpace($filename)) {
            throw "Unsafe artifact filename: $($artifact.id)"
        }
        $destination = [string]$artifact.destination
        if ($destination -notin @(
            "models/checkpoints",
            "models/diffusion_models",
            "models/text_encoders",
            "models/vae"
        )) {
            throw "Unsupported artifact destination: $($artifact.id)"
        }
        $expectedSize = [long]$artifact.size_bytes
        $expectedSha256 = ([string]$artifact.sha256).ToLowerInvariant()
        if ($expectedSize -le 0 -or $expectedSha256 -notmatch "^[0-9a-f]{64}$") {
            throw "Invalid size or checksum: $($artifact.id)"
        }

        $relativeDirectory = $destination.Replace(
            "/",
            [System.IO.Path]::DirectorySeparatorChar
        )
        $directory = Assert-SafeChildPath -Root $rootResolved -Candidate (Join-Path $rootResolved $relativeDirectory) -Label "Artifact directory"
        $target = Assert-SafeChildPath -Root $rootResolved -Candidate (Join-Path $directory $filename) -Label "Artifact file"
        $verified = Get-VerifiedFileState -Path $target -ExpectedSize $expectedSize -ExpectedSha256 $expectedSha256
        if ($null -ne $verified) {
            $results.Add([ordered]@{
                model = [string]$model.family
                scope = [string]$model.benchmark_scope
                artifact = [string]$artifact.id
                status = $verified.status
                path = $verified.path
                size_bytes = $verified.size_bytes
                sha256 = $verified.sha256
            })
            continue
        }

        if (-not $Execute) {
            $results.Add([ordered]@{
                model = [string]$model.family
                scope = [string]$model.benchmark_scope
                artifact = [string]$artifact.id
                status = "planned-download"
                path = $target
                size_bytes = $expectedSize
                sha256 = $expectedSha256
            })
            continue
        }

        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
        $partial = "$target.partial-$([System.Guid]::NewGuid().ToString('N'))"
        try {
            Invoke-WebRequest -Uri $source -OutFile $partial -UseBasicParsing -MaximumRedirection 5

            $partialItem = Get-Item -LiteralPath $partial
            $partialSha256 = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($partialItem.Length -ne $expectedSize -or $partialSha256 -ne $expectedSha256) {
                throw "Downloaded file failed exact size or SHA-256 verification: $($artifact.id)"
            }
            if (Test-Path -LiteralPath $target) {
                throw "Target appeared during download and will not be overwritten: $target"
            }
            Move-Item -LiteralPath $partial -Destination $target
            $installed = Get-VerifiedFileState -Path $target -ExpectedSize $expectedSize -ExpectedSha256 $expectedSha256
            $results.Add([ordered]@{
                model = [string]$model.family
                scope = [string]$model.benchmark_scope
                artifact = [string]$artifact.id
                status = "downloaded-verified"
                path = $installed.path
                size_bytes = $installed.size_bytes
                sha256 = $installed.sha256
            })
        }
        finally {
            if (Test-Path -LiteralPath $partial -PathType Leaf) {
                Remove-Item -LiteralPath $partial
            }
        }
    }
}

[ordered]@{
    ok = $true
    mode = $(if ($Execute) { "execute" } else { "dry-run" })
    manifest = $manifestResolved
    manifest_sha256 = $manifestSha256
    comfyui_root = $rootResolved
    artifact_count = $results.Count
    artifacts = @($results)
} | ConvertTo-Json -Depth 8 -Compress
