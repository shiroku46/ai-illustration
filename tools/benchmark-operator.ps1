[CmdletBinding()]
param(
    [string]$BokePath = "",
    [string]$TsukkomiPath = "",
    [string]$ComfyUIRoot = "",
    [string]$Endpoint = "http://127.0.0.1:8188",
    [string]$Python = "python",

    [switch]$Prepare,
    [switch]$InstallModels,
    [switch]$AcknowledgeExactArtifacts,
    [switch]$AcknowledgeAnimaEvaluationOnly,
    [switch]$ExecuteBenchmark,
    [ValidateRange(1, 144)]
    [int]$MaxRuns = 3,
    [switch]$RetryFailed,
    [switch]$Finalize
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$ReferenceScript = Join-Path $PSScriptRoot "prepare-art-references.ps1"
$InstallScript = Join-Path $PSScriptRoot "install-benchmark-models.ps1"
$PlanPath = Join-Path $RepoRoot "benchmark\model-benchmark-plan.v001.json"
$InstallManifestPath = Join-Path $RepoRoot "benchmark\model-install-manifest.v001.json"
$ReferenceRoot = Join-Path $RepoRoot "local\art-references"
$PackageRoot = Join-Path $RepoRoot "local\benchmark-run-package"
$ResultsRoot = Join-Path $RepoRoot "local\benchmark-results"
$ResultsPath = Join-Path $ResultsRoot "model-benchmark-results.v001.json"
$ContactSheetRoot = Join-Path $RepoRoot "local\benchmark-contact-sheets"
$Stages = [System.Collections.Generic.List[object]]::new()

function Add-Stage {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$State,
        [object]$Detail = $null
    )
    $Stages.Add([ordered]@{
        name = $Name
        state = $State
        detail = $Detail
    })
}

function Convert-JsonOutput {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Text)) {
        throw "$Label returned no JSON output."
    }
    try {
        return $Text | ConvertFrom-Json
    }
    catch {
        throw "$Label returned invalid JSON."
    }
}

function Invoke-PythonJson {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $lines = @(& $Python @Arguments)
    $exitCode = $LASTEXITCODE
    $text = ($lines -join [Environment]::NewLine).Trim()
    $value = Convert-JsonOutput -Text $text -Label $Label
    if ($exitCode -ne 0 -or $value.ok -ne $true) {
        $compact = $value | ConvertTo-Json -Depth 12 -Compress
        throw "$Label failed: $compact"
    }
    return $value
}

function Invoke-RepositoryScriptJson {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Parameters
    )
    $lines = @(& $Path @Parameters)
    $text = ($lines -join [Environment]::NewLine).Trim()
    return Convert-JsonOutput -Text $text -Label $Label
}

function Assert-ExistingDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label must be an existing directory: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Assert-Inputs {
    if ($Prepare) {
        if ([string]::IsNullOrWhiteSpace($BokePath) -or [string]::IsNullOrWhiteSpace($TsukkomiPath)) {
            throw "-Prepare requires both -BokePath and -TsukkomiPath."
        }
    }
    if ($InstallModels) {
        if ([string]::IsNullOrWhiteSpace($ComfyUIRoot)) {
            throw "-InstallModels requires -ComfyUIRoot."
        }
        if (-not $AcknowledgeExactArtifacts) {
            throw "-InstallModels requires -AcknowledgeExactArtifacts."
        }
        if (-not $AcknowledgeAnimaEvaluationOnly) {
            throw "-InstallModels requires -AcknowledgeAnimaEvaluationOnly."
        }
    }
    if ($ExecuteBenchmark) {
        if ([string]::IsNullOrWhiteSpace($ComfyUIRoot)) {
            throw "-ExecuteBenchmark requires -ComfyUIRoot."
        }
        if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) {
            throw "-ExecuteBenchmark requires the deterministic run package. Run with -Prepare first."
        }
    }
    if ($Endpoint -notmatch '^http://(127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?/?$') {
        throw "Endpoint must use an explicit HTTP loopback host."
    }
}

Assert-Inputs
Push-Location $RepoRoot
try {
    # Reference stage: by default report what is already present. Supplying paths permits
    # a checksum-only dry run; -Prepare is the only mode that copies the approved bytes.
    if (-not [string]::IsNullOrWhiteSpace($BokePath) -and -not [string]::IsNullOrWhiteSpace($TsukkomiPath)) {
        $referenceParameters = @{
            BokePath = $BokePath
            TsukkomiPath = $TsukkomiPath
        }
        if ($Prepare) {
            $referenceParameters.Execute = $true
        }
        $referenceResult = Invoke-RepositoryScriptJson -Label "owner reference preparation" -Path $ReferenceScript -Parameters $referenceParameters
        Add-Stage -Name "references" -State $(if ($Prepare) { "prepared" } else { "checked" }) -Detail $referenceResult
    }
    elseif (Test-Path -LiteralPath $ReferenceRoot -PathType Container) {
        Add-Stage -Name "references" -State "present" -Detail ([ordered]@{ path = $ReferenceRoot })
    }
    else {
        Add-Stage -Name "references" -State "needs-input" -Detail ([ordered]@{ required = @("BokePath", "TsukkomiPath") })
    }

    # Run-package stage. Creating the package is local-only and happens only under -Prepare.
    if ($Prepare) {
        if (-not (Test-Path -LiteralPath $ReferenceRoot -PathType Container)) {
            throw "Reference preparation did not create the expected local reference root."
        }
        if (Test-Path -LiteralPath $PackageRoot -PathType Container) {
            $packageResult = Invoke-PythonJson -Label "run package check" -Arguments @(
                "-m", "ai_illustration.benchmark_run_package", "check",
                $PlanPath, $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--reference-root", $ReferenceRoot,
                "--package-root", $PackageRoot
            )
            Add-Stage -Name "run-package" -State "verified-existing" -Detail $packageResult
        }
        else {
            $packageResult = Invoke-PythonJson -Label "run package prepare" -Arguments @(
                "-m", "ai_illustration.benchmark_run_package", "prepare",
                $PlanPath, $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--reference-root", $ReferenceRoot,
                "--package-root", $PackageRoot
            )
            Add-Stage -Name "run-package" -State "prepared" -Detail $packageResult
        }
    }
    elseif (Test-Path -LiteralPath $PackageRoot -PathType Container) {
        if (Test-Path -LiteralPath $ReferenceRoot -PathType Container) {
            $packageResult = Invoke-PythonJson -Label "run package check" -Arguments @(
                "-m", "ai_illustration.benchmark_run_package", "check",
                $PlanPath, $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--reference-root", $ReferenceRoot,
                "--package-root", $PackageRoot
            )
            Add-Stage -Name "run-package" -State "verified" -Detail $packageResult
        }
        else {
            Add-Stage -Name "run-package" -State "blocked" -Detail ([ordered]@{ reason = "local references are unavailable" })
        }
    }
    else {
        Add-Stage -Name "run-package" -State "not-prepared"
    }

    # Model stage. The reviewed installer owns all download behavior. This coordinator
    # never contains a downloader. Without -InstallModels it is checksum-only planning.
    if (-not [string]::IsNullOrWhiteSpace($ComfyUIRoot)) {
        $resolvedComfy = Assert-ExistingDirectory -Path $ComfyUIRoot -Label "ComfyUIRoot"
        $installParameters = @{ ComfyUIRoot = $resolvedComfy }
        if ($InstallModels) {
            $installParameters.Execute = $true
            $installParameters.AcknowledgeExactArtifacts = $true
            $installParameters.AcknowledgeAnimaEvaluationOnly = $true
        }
        $installResult = Invoke-RepositoryScriptJson -Label "model installation" -Path $InstallScript -Parameters $installParameters
        Add-Stage -Name "models" -State $(if ($InstallModels) { "installed-or-verified" } else { "checked" }) -Detail $installResult

        try {
            $offlineResult = Invoke-PythonJson -Label "offline readiness" -Arguments @(
                "-m", "ai_illustration.benchmark_readiness", "offline-check",
                $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--comfyui-root", $resolvedComfy
            )
            Add-Stage -Name "offline-readiness" -State "ready" -Detail $offlineResult
        }
        catch {
            if ($InstallModels -or $ExecuteBenchmark) {
                throw
            }
            Add-Stage -Name "offline-readiness" -State "not-ready" -Detail ([ordered]@{
                reason = $_.Exception.Message
            })
        }
    }
    else {
        $resolvedComfy = $null
        Add-Stage -Name "models" -State "needs-input" -Detail ([ordered]@{ required = "ComfyUIRoot" })
    }

    # Execution stage. Status is read-only. Actual prompts require -ExecuteBenchmark and
    # the executor repeats runtime readiness before it can queue the first pending run.
    if (Test-Path -LiteralPath $PackageRoot -PathType Container) {
        $statusResult = Invoke-PythonJson -Label "benchmark status" -Arguments @(
            "-m", "ai_illustration.benchmark_execute", "status",
            $PackageRoot, $PlanPath, $InstallManifestPath,
            "--workspace-root", $RepoRoot,
            "--results-root", $ResultsRoot
        )
        Add-Stage -Name "benchmark-status" -State "checked" -Detail $statusResult

        if ($ExecuteBenchmark) {
            $runtimeResult = Invoke-PythonJson -Label "runtime readiness" -Arguments @(
                "-m", "ai_illustration.benchmark_readiness", "runtime-check",
                $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--comfyui-root", $resolvedComfy,
                "--endpoint", $Endpoint
            )
            Add-Stage -Name "runtime-readiness" -State "ready" -Detail $runtimeResult

            $executeArguments = @(
                "-m", "ai_illustration.benchmark_execute", "run",
                $PackageRoot, $PlanPath, $InstallManifestPath,
                "--workspace-root", $RepoRoot,
                "--results-root", $ResultsRoot,
                "--comfyui-root", $resolvedComfy,
                "--endpoint", $Endpoint,
                "--execute",
                "--max-runs", [string]$MaxRuns
            )
            if ($RetryFailed) {
                $executeArguments += "--retry-failed"
            }
            $executionResult = Invoke-PythonJson -Label "benchmark execution" -Arguments $executeArguments
            Add-Stage -Name "benchmark-execution" -State "executed" -Detail $executionResult
            $statusResult = $executionResult
        }
        else {
            Add-Stage -Name "benchmark-execution" -State "not-requested"
        }
    }
    else {
        $statusResult = $null
        Add-Stage -Name "benchmark-status" -State "blocked" -Detail ([ordered]@{ reason = "run package is unavailable" })
        Add-Stage -Name "benchmark-execution" -State "blocked"
    }

    # Contact sheets are generated only from a complete attempted matrix. No model
    # decision is made here; the output is owner-review material only.
    if ($Finalize) {
        if ($null -eq $statusResult -or $statusResult.pending -ne 0) {
            throw "-Finalize requires zero pending benchmark runs."
        }
        if (-not (Test-Path -LiteralPath $ResultsPath -PathType Leaf)) {
            throw "-Finalize requires the aggregate benchmark results file."
        }
        if (Test-Path -LiteralPath $ContactSheetRoot) {
            throw "Contact-sheet output already exists and will not be overwritten: $ContactSheetRoot"
        }
        $contactResult = Invoke-PythonJson -Label "contact sheet rendering" -Arguments @(
            "-m", "ai_illustration.benchmark_results", "render-contact-sheets",
            $ResultsPath, $PlanPath,
            "--workspace-root", $RepoRoot,
            "--reference-root", $ReferenceRoot,
            "--result-root", $ResultsRoot,
            "--output-dir", $ContactSheetRoot
        )
        Add-Stage -Name "contact-sheets" -State "rendered" -Detail $contactResult
    }
    elseif (Test-Path -LiteralPath $ContactSheetRoot -PathType Container) {
        Add-Stage -Name "contact-sheets" -State "present" -Detail ([ordered]@{ path = $ContactSheetRoot })
    }
    else {
        Add-Stage -Name "contact-sheets" -State "not-requested"
    }

    [ordered]@{
        ok = $true
        repository_root = $RepoRoot
        effect_requests = [ordered]@{
            prepare = [bool]$Prepare
            install_models = [bool]$InstallModels
            execute_benchmark = [bool]$ExecuteBenchmark
            retry_failed = [bool]$RetryFailed
            finalize = [bool]$Finalize
            max_runs = $MaxRuns
        }
        stages = @($Stages)
        automatic_selection = $false
    } | ConvertTo-Json -Depth 16 -Compress
}
finally {
    Pop-Location
}
