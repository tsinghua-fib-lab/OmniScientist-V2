<#
.SYNOPSIS
  Ownership-aware OmniScientist uninstall wrapper for Windows PowerShell.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File cli\scripts\uninstall.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File cli\scripts\uninstall.ps1 -Everything -Yes
#>

[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$DryRun,
    [switch]$Purge,
    [switch]$AllProjectData,
    [switch]$AllInstallations,
    [switch]$Everything,
    [switch]$KeepProgram,
    [switch]$Yes,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command omni -ErrorAction SilentlyContinue)) {
    Write-Error "The omni command is not on PATH. Activate the environment that contains OmniScientist, then run 'omni uninstall --dry-run'."
    exit 1
}

$OmniArgs = @("uninstall")
if ($DryRun) { $OmniArgs += "--dry-run" }
if ($Purge) { $OmniArgs += "--purge" }
if ($AllProjectData) { $OmniArgs += "--all-project-data" }
if ($AllInstallations) { $OmniArgs += "--all-installations" }
if ($Everything) { $OmniArgs += "--everything" }
if ($KeepProgram) { $OmniArgs += "--keep-program" }
if ($Yes) { $OmniArgs += "--yes" }
if ($Json) { $OmniArgs += "--json" }

& omni @OmniArgs
exit $LASTEXITCODE
