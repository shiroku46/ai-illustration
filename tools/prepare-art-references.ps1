[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BokePath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TsukkomiPath,

    [string]$ReferenceRoot = "",
    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ReferenceRoot)) {
    $ReferenceRoot = Join-Path $PSScriptRoot "..\local\art-references"
}

$specifications = @(
    [ordered]@{
        role = "boke"
        source = $BokePath
        filename = "boke-rakuko.png"
        sha256 = "5d5d67ecca13eebfb762b8251ea0bb00481951d79dcd46c9e44986fc2d069e69"
    },
    [ordered]@{
        role = "tsukkomi"
        source = $TsukkomiPath
        filename = "tsukkomi-sakura.png"
        sha256 = "474465adea571e35a1c722fe96e910f75bdf919f43927cb2ba366186ea672303"
    }
)

$rootFull = [System.IO.Path]::GetFullPath($ReferenceRoot)
if (Test-Path -LiteralPath $rootFull) {
    if (-not (Test-Path -LiteralPath $rootFull -PathType Container)) {
        throw "ReferenceRoot exists but is not a directory."
    }
}

$results = [System.Collections.Generic.List[object]]::new()
foreach ($specification in $specifications) {
    $source = (Resolve-Path -LiteralPath $specification.source).Path
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Reference source must be a regular file: $source"
    }
    $sourceItem = Get-Item -LiteralPath $source
    $sourceSha256 = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceSha256 -ne $specification.sha256) {
        throw "Reference SHA-256 mismatch for $($specification.role): $sourceSha256"
    }

    $target = [System.IO.Path]::GetFullPath((Join-Path $rootFull $specification.filename))
    $prefix = $rootFull.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Reference target escapes ReferenceRoot."
    }

    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $targetSha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($targetSha256 -ne $specification.sha256) {
            throw "Existing reference does not match the approved bytes: $target"
        }
        $results.Add([ordered]@{
            role = $specification.role
            status = "present-verified"
            source = $source
            target = $target
            size_bytes = [long](Get-Item -LiteralPath $target).Length
            sha256 = $targetSha256
        })
        continue
    }

    if (-not $Execute) {
        $results.Add([ordered]@{
            role = $specification.role
            status = "planned-copy"
            source = $source
            target = $target
            size_bytes = [long]$sourceItem.Length
            sha256 = $sourceSha256
        })
        continue
    }

    if (-not (Test-Path -LiteralPath $rootFull -PathType Container)) {
        New-Item -ItemType Directory -Path $rootFull | Out-Null
    }
    $partial = "$target.partial-$([System.Guid]::NewGuid().ToString('N'))"
    try {
        Copy-Item -LiteralPath $source -Destination $partial
        $partialSha256 = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($partialSha256 -ne $specification.sha256) {
            throw "Copied reference failed SHA-256 verification: $($specification.role)"
        }
        if (Test-Path -LiteralPath $target) {
            throw "Reference target appeared during copy and will not be overwritten: $target"
        }
        Move-Item -LiteralPath $partial -Destination $target
        $results.Add([ordered]@{
            role = $specification.role
            status = "copied-verified"
            source = $source
            target = $target
            size_bytes = [long](Get-Item -LiteralPath $target).Length
            sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        })
    }
    finally {
        if (Test-Path -LiteralPath $partial -PathType Leaf) {
            Remove-Item -LiteralPath $partial
        }
    }
}

[ordered]@{
    ok = $true
    mode = $(if ($Execute) { "execute" } else { "dry-run" })
    reference_root = $rootFull
    reference_count = $results.Count
    references = @($results)
} | ConvertTo-Json -Depth 6 -Compress