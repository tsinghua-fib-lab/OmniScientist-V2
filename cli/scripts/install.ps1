<#
.SYNOPSIS
  Ownership-aware OmniScientist installer for Windows PowerShell.

.DESCRIPTION
  The default is always an isolated uv tool. Missing uv installations are
  bootstrapped with Astral's official installer. Active Conda and virtualenv
  variables are ignored unless -Method env is explicitly selected. A source
  checkout uses a local snapshot; a standalone installer (piped `irm … | iex`,
  no source tree) installs the published PyPI package by default. -Channel
  master is an explicit moving development channel; -Remote -Ref
  <tag-or-commit> stays immutable/pinned.

  Install == update for a checkout: rerun this script to deploy its current
  tree. Stable remote users install from PyPI and run `omni update`.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File cli\scripts\install.ps1
  cli\scripts\install.ps1 -Channel master
  cli\scripts\install.ps1 -Channel pypi
  cli\scripts\install.ps1 -Local
  cli\scripts\install.ps1 -Method env
  cli\scripts\install.ps1 -Method env -ForceCondaBase
  cli\scripts\install.ps1 -OnConflict migrate
  cli\scripts\install.ps1 -Extras "mcp,vec,channels"
  cli\scripts\install.ps1 -IndexUrl pypi
  cli\scripts\install.ps1 -Remote -Ref <tag-or-commit>
#>

[CmdletBinding()]
param(
    [switch]$Editable,
    [ValidateSet("uv", "env", "auto")]
    [string]$Method = "uv",
    [switch]$ForceCondaBase,
    [ValidateSet("ask", "upgrade", "migrate", "cancel")]
    [string]$OnConflict = "ask",
    [string]$Extras = "mcp,vec,channels",
    [string]$IndexUrl = "",
    [switch]$Local,
    [switch]$Pypi,
    [switch]$Remote,
    [ValidateSet("", "master", "pypi")]
    [string]$Channel = "",
    [string]$From = "",
    [string]$Ref = ""
)

$ErrorActionPreference = "Stop"
$TrackBranch = $false
$DefaultTrackBranch = "master"
$InstalledMethod = ""
$InstalledOmni = ""
$InstalledPython = ""
$EnvPythonOverride = ""
$MigrateAfter = $false
$UvToolDirOverride = ""
$UvBinDirOverride = ""
$DetectedUvBin = ""
$MigrationCleanupFailed = $false
$AliyunPypiIndexUrl = "https://mirrors.aliyun.com/pypi/simple/"
$OfficialPypiIndexUrl = "https://pypi.org/simple/"

if ([string]::IsNullOrWhiteSpace($IndexUrl)) {
    $IndexUrl = if ($env:OMNI_PYPI_INDEX_URL) { $env:OMNI_PYPI_INDEX_URL } else { $OfficialPypiIndexUrl }
}
switch ($IndexUrl.ToLowerInvariant()) {
    "aliyun" { $IndexUrl = $AliyunPypiIndexUrl }
    "pypi" { $IndexUrl = $OfficialPypiIndexUrl }
    "official" { $IndexUrl = $OfficialPypiIndexUrl }
}
if ($IndexUrl -notmatch '^(https?|file)://') {
    Write-Error "Invalid -IndexUrl: expected aliyun, pypi, or an http(s)/file URL."
    exit 2
}
$UvIndexArgs = @("--default-index", $IndexUrl)
$PipIndexArgs = @("--index-url", $IndexUrl)

$RepoDir = $null
try { $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path } catch { $RepoDir = $null }

if ($Method -eq "auto") {
    Write-Warning "-Method auto is deprecated and now means -Method uv."
    $Method = "uv"
}
if ($ForceCondaBase -and $Method -ne "env") {
    Write-Error "-ForceCondaBase is only valid with -Method env."
    exit 2
}

$sourceFlags = @($Local.IsPresent, $Pypi.IsPresent, $Remote.IsPresent) | Where-Object { $_ }
if ($sourceFlags.Count -gt 1) {
    Write-Error "Choose only one source: -Local, -Pypi, or -Remote."
    exit 2
}

if (-not $Channel -and $env:OMNI_INSTALL_CHANNEL) {
    if ($env:OMNI_INSTALL_CHANNEL -notin @("master", "pypi")) {
        Write-Error "Unknown OMNI_INSTALL_CHANNEL: $($env:OMNI_INSTALL_CHANNEL) (choose master or pypi)."
        exit 2
    }
    $Channel = $env:OMNI_INSTALL_CHANNEL
}

$SourceMode = if ($Local) { "local" } elseif ($Pypi) { "pypi" } elseif ($Remote) { "git" } else { "auto" }

# A tracking "channel" is a convenience over the raw source modes: it selects
# where a *user* install pulls from and how `omni update` advances. An explicit
# source flag (-Local/-Pypi/-Remote/-From) always wins over -Channel.
if ($Channel -and $SourceMode -eq "auto") {
    switch ($Channel) {
        "master" { $SourceMode = "git"; $TrackBranch = $true; if ([string]::IsNullOrWhiteSpace($Ref)) { $Ref = $DefaultTrackBranch } }
        "pypi" { $SourceMode = "pypi" }
    }
}

if ($SourceMode -eq "auto") {
    $versionFile = if ($RepoDir) { Join-Path $RepoDir "src\omni\__init__.py" } else { "" }
    $isCheckout = $RepoDir -and (Test-Path (Join-Path $RepoDir "pyproject.toml")) -and (Test-Path $versionFile)
    if ($isCheckout) {
        $SourceMode = "local"
    } else {
        # A standalone installer is a stable user install. The moving master
        # channel remains available only when selected explicitly.
        $SourceMode = "pypi"
        if (-not $Channel) { $Channel = "pypi" }
    }
}

switch ($SourceMode) {
    "local" {
        if (-not $RepoDir -or -not (Test-Path (Join-Path $RepoDir "pyproject.toml"))) {
            Write-Error "-Local requires an OmniScientist source checkout."
            exit 2
        }
        $Spec = if ($Extras) { "$RepoDir[$Extras]" } else { $RepoDir }
        Write-Host "-> Source: local checkout snapshot ($RepoDir)"
    }
    "pypi" {
        if ($Editable) { Write-Error "-Editable requires -Local."; exit 2 }
        $Spec = if ($Extras) { "OmniScientist-V2[$Extras]" } else { "OmniScientist-V2" }
        Write-Host "-> Source: published PyPI package"
    }
    "git" {
        if ($Editable) { Write-Error "Remote installs do not support -Editable; use -Local."; exit 2 }
        if ([string]::IsNullOrWhiteSpace($Ref)) {
            Write-Error "Remote installation requires -Ref <immutable-tag-or-commit> (or -Channel master)."
            exit 2
        }
        if ($TrackBranch) {
            Write-Host "-> Tracking channel '$Ref' (moving branch tip; non-reproducible)."
            Write-Host "   Pin with -Remote -Ref <tag-or-commit> for a reproducible install."
        } elseif ($Ref -notmatch '^([0-9a-fA-F]{40}|v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?)$') {
            Write-Error "Ref '$Ref' is not an immutable release tag or full 40-character commit hash (use -Channel master to track a branch)."
            exit 2
        }
        if ([string]::IsNullOrWhiteSpace($From)) {
            Write-Error "Remote Git installation requires -From <official-github-repository-url>."
            exit 2
        }
        $gitSpec = "git+${From}@${Ref}#subdirectory=cli"
        $Spec = if ($Extras) { "OmniScientist-V2[$Extras] @ $gitSpec" } else { "OmniScientist-V2 @ $gitSpec" }
        if ($TrackBranch) { Write-Host "-> Source: git channel ($gitSpec)" } else { Write-Host "-> Source: immutable git ref ($gitSpec)" }
    }
}

Write-Host "-> Python package index: $IndexUrl"

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-PipxEnvironmentValue([string]$Name) {
    if (-not (Test-Command "pipx")) { return "" }
    $value = & pipx environment --value $Name 2>$null
    if ($LASTEXITCODE -ne 0) { return "" }
    return (@($value) -join "").Trim()
}

function Assert-NativeSuccess([string]$Action) {
    $code = $LASTEXITCODE
    if ($null -ne $code -and $code -ne 0) {
        throw "$Action failed (exit=$code)."
    }
}

$script:InstallLockStream = $null

function Acquire-InstallLock {
    $stateDir = Get-InstallStateDir
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    $lockPath = Join-Path $stateDir "installer.lock"
    try {
        $script:InstallLockStream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw "Another Omni installation is already running (lock: $lockPath)."
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes("$PID`n")
    $script:InstallLockStream.SetLength(0)
    $script:InstallLockStream.Write($bytes, 0, $bytes.Length)
    $script:InstallLockStream.Flush()
}

function Release-InstallLock {
    if ($null -ne $script:InstallLockStream) {
        $script:InstallLockStream.Dispose()
        $script:InstallLockStream = $null
    }
}

function Get-InstallStateDir {
    if ($env:OMNI_INSTALL_STATE_DIR) { return $env:OMNI_INSTALL_STATE_DIR }
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA "OmniScientist\state") }
    return (Join-Path $HOME "AppData\Local\OmniScientist\state")
}

function Wait-PreviousUninstall {
    $stateDir = Get-InstallStateDir
    $pending = Join-Path $stateDir "uninstall.pending"
    $failed = Join-Path $stateDir "uninstall.failed"
    $waitSeconds = 30
    if ($env:OMNI_INSTALL_WAIT_SECONDS) {
        if (-not [int]::TryParse($env:OMNI_INSTALL_WAIT_SECONDS, [ref]$waitSeconds) -or $waitSeconds -le 0) {
            throw "OMNI_INSTALL_WAIT_SECONDS must be a positive integer."
        }
    }
    if (Test-Path $pending) {
        Write-Host "-> Waiting for the previous Omni uninstall to finish"
        $deadline = [DateTime]::UtcNow.AddSeconds($waitSeconds)
        while ((Test-Path $pending) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 100
        }
        if (Test-Path $pending) {
            throw "Previous Omni uninstall did not finish within ${waitSeconds}s. Inspect or remove the stale marker after confirming no uninstall is running: $pending"
        }
        Write-Host "-> Previous Omni uninstall finished"
    }
    if (Test-Path $failed) {
        Write-Warning "The previous program removal reported an error; reinstall will repair the uv tool. Failure record: $failed"
    }
}

function Invoke-OmniVersionProbe([string]$Launcher) {
    $timeoutSeconds = 3
    if ($env:OMNI_INSTALL_PROBE_TIMEOUT_SECONDS) {
        if (-not [int]::TryParse($env:OMNI_INSTALL_PROBE_TIMEOUT_SECONDS, [ref]$timeoutSeconds) -or $timeoutSeconds -le 0) {
            throw "OMNI_INSTALL_PROBE_TIMEOUT_SECONDS must be a positive integer."
        }
    }
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Launcher
    $startInfo.Arguments = "--version"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $null }
        if (-not $process.WaitForExit($timeoutSeconds * 1000)) {
            try { $process.Kill() } catch { }
            Write-Warning "Timed out checking old Omni launcher after ${timeoutSeconds}s: $Launcher"
            return $null
        }
        $output = $process.StandardOutput.ReadToEnd()
        return @($output -split "`r?`n" | Where-Object { $_ }) | Select-Object -First 1
    } catch {
        return $null
    } finally {
        $process.Dispose()
    }
}

function Get-ActivePython {
    if ($env:VIRTUAL_ENV) {
        $py = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
        if (Test-Path $py) { return $py }
    }
    if ($env:CONDA_PREFIX) {
        $py = Join-Path $env:CONDA_PREFIX "python.exe"
        if (Test-Path $py) { return $py }
    }
    return $null
}

function Get-PythonOwner([string]$Launcher) {
    $launcherDir = Split-Path $Launcher -Parent
    foreach ($candidate in @(
        (Join-Path $launcherDir "python.exe"),
        (Join-Path (Split-Path $launcherDir -Parent) "python.exe")
    )) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    return ""
}

function Get-PythonInstallMethod([string]$Python) {
    if (-not $Python -or -not (Test-Path $Python)) { return "" }
    $prefix = Split-Path $Python -Parent
    if ((Split-Path $prefix -Leaf) -eq "Scripts") {
        $prefix = Split-Path $prefix -Parent
    }
    if (Test-Path (Join-Path $prefix "uv-receipt.toml")) { return "uv" }
    if (Test-Path (Join-Path $prefix "pipx_metadata.json")) { return "pipx" }
    return ""
}

function Get-PythonPrefix([string]$Python) {
    $prefix = Split-Path $Python -Parent
    if ((Split-Path $prefix -Leaf) -eq "Scripts") {
        $prefix = Split-Path $prefix -Parent
    }
    return $prefix
}

function Get-OmniInstallations {
    $rows = @()
    $seen = @{}
    $uvBin = ""
    if (Test-Command "uv") {
        try { $uvBin = (& uv tool dir --bin).Trim() } catch { $uvBin = "" }
    }
    $script:DetectedUvBin = $uvBin
    $pipxHome = Get-PipxEnvironmentValue "PIPX_HOME"
    $pipxBin = Get-PipxEnvironmentValue "PIPX_BIN_DIR"
    $pipxPython = if ($pipxHome) {
        Join-Path $pipxHome "venvs\omniscientist-v2\Scripts\python.exe"
    } else {
        ""
    }
    $candidatePaths = @(
        @(Get-Command omni -All -ErrorAction SilentlyContinue) |
            ForEach-Object { $_.Source } |
            Where-Object { $_ }
    )
    if ($uvBin) {
        foreach ($name in @("omni.exe", "omni")) {
            $candidate = Join-Path $uvBin $name
            if (Test-Path $candidate) { $candidatePaths += $candidate }
        }
    }
    if ($pipxBin) {
        foreach ($name in @("omni.exe", "omni")) {
            $candidate = Join-Path $pipxBin $name
            if (Test-Path $candidate) { $candidatePaths += $candidate }
        }
    }
    foreach ($path in $candidatePaths) {
        if (-not $path -or $seen.ContainsKey($path)) { continue }
        $seen[$path] = $true
        $version = Invoke-OmniVersionProbe $path
        if (-not $version) { continue }
        if ($version -notmatch "OmniScientist") { continue }
        $launcherDir = Split-Path $path -Parent
        $python = if ($pipxBin -and $launcherDir -eq $pipxBin -and (Test-Path $pipxPython)) {
            (Resolve-Path $pipxPython).Path
        } else {
            Get-PythonOwner $path
        }
        $normalized = $path.Replace("\", "/").ToLowerInvariant()
        $method = if ($uvBin -and ((Split-Path $path -Parent) -eq $uvBin)) {
            "uv"
        } elseif (
            ($pipxBin -and $launcherDir -eq $pipxBin) -or
            (Get-PythonInstallMethod $python) -eq "pipx" -or
            $normalized -match "/pipx/venvs/omniscientist-v2/" -or
            $normalized -match "/pipx/venvs/omniscientist/"
        ) {
            "pipx"
        } elseif ((Get-PythonInstallMethod $python) -eq "uv") {
            "uv"
        } else {
            "env"
        }
        $rows += [pscustomobject]@{ Path = $path; Method = $method; Python = $python; Version = $version }
    }
    return $rows
}

function Resolve-DuplicateInstallations([array]$Installations) {
    if ($Installations.Count -eq 0) { return }
    Write-Host ""
    Write-Host "Existing OmniScientist installation(s) detected:"
    for ($i = 0; $i -lt $Installations.Count; $i++) {
        $row = $Installations[$i]
        Write-Host "  $($i + 1)) $($row.Path) [$($row.Method); $($row.Version)]"
    }
    $action = $OnConflict
    if ($action -eq "ask" -and $Installations.Count -eq 1 -and
        $Installations[0].Method -eq "uv" -and $Method -eq "uv") {
        $action = "upgrade"
        Write-Host "-> Existing uv installation will be upgraded in place"
    }
    if ($action -eq "ask") {
        if ([Console]::IsInputRedirected) {
            Write-Error "Installation stopped: choose upgrade, migrate, or cancel with -OnConflict. Example: -OnConflict migrate"
            exit 2
        }
        $choice = Read-Host "Choose [1] upgrade existing, [2] migrate to uv, [3] cancel"
        $action = switch ($choice) { "1" { "upgrade" } "upgrade" { "upgrade" } "2" { "migrate" } "migrate" { "migrate" } default { "cancel" } }
    }
    switch ($action) {
        "cancel" { Write-Host "Installation cancelled; no changes were made."; exit 0 }
        "migrate" {
            $unbound = @(
                $Installations |
                    Where-Object {
                        $_.Method -eq "env" -and
                        (-not $_.Python -or -not (Test-Path $_.Python))
                    }
            )
            if ($unbound.Count -gt 0) {
                Write-Error "Cannot safely migrate $($unbound[0].Path) because its owner cannot be bound. Remove that launcher manually or choose cancel; no installation changes were made."
                exit 2
            }
            $script:Method = "uv"
            $script:MigrateAfter = $true
        }
        "upgrade" {
            $first = $Installations[0]
            if ($first.Method -eq "uv") {
                $script:Method = "uv"
                if ($first.Python -and (Test-Path $first.Python)) {
                    $prefix = Get-PythonPrefix $first.Python
                    $script:UvToolDirOverride = Split-Path $prefix -Parent
                    $script:UvBinDirOverride = Split-Path $first.Path -Parent
                }
                elseif (
                    -not $DetectedUvBin -or
                    (Split-Path $first.Path -Parent) -ne $DetectedUvBin
                ) {
                    Write-Error "Cannot bind the existing uv registry for $($first.Path); choose migrate or cancel."
                    exit 2
                }
            }
            elseif ($first.Method -eq "pipx") {
                Write-Error "The existing launcher is owned by pipx; this repository installer will not mutate its managed environment. Use 'omni update' for the published install, or choose migrate to consolidate into uv."
                exit 2
            }
            elseif ($first.Python -and (Test-Path $first.Python)) {
                $script:Method = "env"
                $script:EnvPythonOverride = $first.Python
            }
            else {
                Write-Error "Cannot identify the Python owner of $($first.Path); choose migrate or cancel."
                exit 2
            }
            if ($Installations.Count -gt 1) {
                Write-Warning "Upgrade targets the first PATH installation; other copies remain. Use migrate to consolidate."
            }
        }
    }
}

function Test-CondaBasePython([string]$Python) {
    if ($env:CONDA_PREFIX -and $env:CONDA_DEFAULT_ENV -eq "base") {
        if ((Join-Path $env:CONDA_PREFIX "python.exe") -eq $Python) { return $true }
    }
    if (Test-Command "conda") {
        try {
            $base = (& conda info --base).Trim()
            if ($base -and (Join-Path $base "python.exe") -eq $Python) { return $true }
        } catch { }
    }
    return $false
}

function Install-IntoEnv([string]$Python) {
    if ((Test-CondaBasePython $Python) -and -not $ForceCondaBase) {
        Write-Error "Refusing to install OmniScientist into Conda base. Use uv, a dedicated env, or explicitly add -ForceCondaBase."
        exit 2
    }
    Write-Host "-> Installing into explicitly selected Python env: $Python"
    # Compile .pyc at install time (uv skips it by default) so the first launch
    # and the research-pptx setup step don't pay a cold bytecode-compile pause.
    $env:UV_COMPILE_BYTECODE = "1"
    $editableArgs = @(); if ($Editable) { $editableArgs = @("--editable") }
    if (Test-Command "uv") {
        if ($Editable) {
            & uv pip install --python $Python @UvIndexArgs @editableArgs $Spec
        } elseif ($SourceMode -eq "local") {
            & uv pip install --python $Python @UvIndexArgs --reinstall-package OmniScientist-V2 $Spec
        } elseif ($TrackBranch) {
            & uv pip install --python $Python --refresh --reinstall-package OmniScientist-V2 @UvIndexArgs $Spec
        } else {
            & uv pip install --python $Python @UvIndexArgs $Spec
        }
    } else {
        if ($Editable) {
            & $Python -m pip install @PipIndexArgs @editableArgs $Spec
        } elseif ($SourceMode -eq "local" -or $TrackBranch) {
            & $Python -m pip install @PipIndexArgs --force-reinstall $Spec
        } else {
            & $Python -m pip install @PipIndexArgs $Spec
        }
    }
    Assert-NativeSuccess "Installing OmniScientist into $Python"
    $scriptsOutput = & $Python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    Assert-NativeSuccess "Resolving the selected Python scripts directory"
    $scripts = $scriptsOutput.Trim()
    $script:InstalledMethod = "env"
    $script:InstalledPython = $Python
    $script:InstalledOmni = Join-Path $scripts "omni.exe"
}

function Install-UvTool {
    $uvCommand = Get-OrInstallUv
    $previousToolDir = $env:UV_TOOL_DIR
    $previousBinDir = $env:UV_TOOL_BIN_DIR
    try {
        if ($UvToolDirOverride) {
            $env:UV_TOOL_DIR = $UvToolDirOverride
            $env:UV_TOOL_BIN_DIR = $UvBinDirOverride
        }
        Write-Host "-> Installing with uv tool into an isolated environment"
        $editableArgs = @(); if ($Editable) { $editableArgs = @("--editable") }
        if ($Editable) {
            & $uvCommand tool install --force @UvIndexArgs @editableArgs $Spec
        } elseif ($SourceMode -eq "local") {
            & $uvCommand tool install --force @UvIndexArgs --reinstall-package OmniScientist-V2 $Spec
        } elseif ($TrackBranch) {
            # --refresh defeats uv's git cache so the moving branch tip is re-resolved.
            & $uvCommand tool install --force --refresh @UvIndexArgs $Spec
        } else {
            & $uvCommand tool install --force @UvIndexArgs $Spec
        }
        Assert-NativeSuccess "Installing the OmniScientist uv tool"
        & $uvCommand tool update-shell
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "uv tool update-shell failed; the tool is installed but a new terminal may not find it."
        }
        $uvBinOutput = & $uvCommand tool dir --bin
        Assert-NativeSuccess "Resolving the uv tool executable directory"
        $uvBin = $uvBinOutput.Trim()
        $script:InstalledMethod = "uv"
        $script:InstalledOmni = Join-Path $uvBin "omni.exe"
        if (-not (Test-Path $script:InstalledOmni)) {
            $script:InstalledOmni = Join-Path $uvBin "omni"
        }
        $script:InstalledPython = Get-PythonOwner $script:InstalledOmni
        Write-Host "(uv bin: $uvBin; reopen the terminal if PATH has not refreshed.)"
    } finally {
        if ($null -eq $previousToolDir) { Remove-Item Env:UV_TOOL_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_DIR = $previousToolDir }
        if ($null -eq $previousBinDir) { Remove-Item Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_BIN_DIR = $previousBinDir }
    }
}

function Get-OrInstallUv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    Write-Host "-> uv not found; installing it with Astral's official installer"
    & powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host
    Assert-NativeSuccess "Installing uv"

    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @()
    if ($env:UV_INSTALL_DIR) { $candidates += (Join-Path $env:UV_INSTALL_DIR "uv.exe") }
    $candidates += (Join-Path $HOME ".local\bin\uv.exe")
    $candidates += (Join-Path $HOME ".cargo\bin\uv.exe")
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }

    Write-Error "uv installation completed, but its executable could not be located. Open a new terminal or install uv manually: https://docs.astral.sh/uv/"
    exit 1
}

function Remove-PreviousInstallations([array]$Installations) {
    if (-not $MigrateAfter) { return }
    foreach ($row in $Installations) {
        if ($row.Python -and $row.Python -eq $InstalledPython) { continue }
        if ($row.Method -eq "env" -and $row.Path -eq $InstalledOmni) { continue }
        if ($row.Python -and (Test-Path $row.Python)) {
            Write-Host "-> Removing previous $($row.Method) installation owned by $($row.Python)"
            if ($row.Method -eq "pipx") {
                if (-not (Test-Command "pipx")) {
                    Write-Warning "pipx owns $($row.Path) but is not on PATH; it was left in place."
                    $script:MigrationCleanupFailed = $true
                    continue
                }
                # <PIPX_HOME>\venvs\omniscientist-v2\Scripts\python.exe
                $pipxHome = Split-Path (Split-Path (Split-Path (Split-Path $row.Python -Parent) -Parent) -Parent) -Parent
                $previousHome = $env:PIPX_HOME
                $previousBin = $env:PIPX_BIN_DIR
                $previousMan = $env:PIPX_MAN_DIR
                $temporaryBin = ""
                try {
                    $env:PIPX_HOME = $pipxHome
                    $env:PIPX_MAN_DIR = Join-Path $pipxHome "man"
                    if ($row.Path -eq $InstalledOmni) {
                        $temporaryBin = Join-Path ([System.IO.Path]::GetTempPath()) "omni-pipx-cleanup-$([guid]::NewGuid())"
                        New-Item -ItemType Directory -Force -Path $temporaryBin | Out-Null
                        $env:PIPX_BIN_DIR = $temporaryBin
                    } else {
                        $env:PIPX_BIN_DIR = Split-Path $row.Path -Parent
                    }
                    & pipx uninstall OmniScientist-V2
                    if ($LASTEXITCODE -ne 0) {
                        Write-Warning "Could not remove $($row.Path) (pipx exit=$LASTEXITCODE)."
                        $script:MigrationCleanupFailed = $true
                    }
                } finally {
                    if ($null -eq $previousHome) { Remove-Item Env:PIPX_HOME -ErrorAction SilentlyContinue } else { $env:PIPX_HOME = $previousHome }
                    if ($null -eq $previousBin) { Remove-Item Env:PIPX_BIN_DIR -ErrorAction SilentlyContinue } else { $env:PIPX_BIN_DIR = $previousBin }
                    if ($null -eq $previousMan) { Remove-Item Env:PIPX_MAN_DIR -ErrorAction SilentlyContinue } else { $env:PIPX_MAN_DIR = $previousMan }
                    if ($temporaryBin) { Remove-Item -Recurse -Force $temporaryBin -ErrorAction SilentlyContinue }
                }
            } elseif ($row.Method -eq "uv") {
                if (-not (Test-Command "uv")) {
                    Write-Warning "uv owns $($row.Path) but is not on PATH; it was left in place."
                    $script:MigrationCleanupFailed = $true
                    continue
                }
                $previousHome = $env:UV_TOOL_DIR
                $previousBin = $env:UV_TOOL_BIN_DIR
                $temporaryBin = ""
                try {
                    $prefix = Get-PythonPrefix $row.Python
                    $env:UV_TOOL_DIR = Split-Path $prefix -Parent
                    if ($row.Path -eq $InstalledOmni) {
                        $temporaryBin = Join-Path ([System.IO.Path]::GetTempPath()) "omni-uv-cleanup-$([guid]::NewGuid())"
                        New-Item -ItemType Directory -Force -Path $temporaryBin | Out-Null
                        $env:UV_TOOL_BIN_DIR = $temporaryBin
                    } else {
                        $env:UV_TOOL_BIN_DIR = Split-Path $row.Path -Parent
                    }
                    & uv tool uninstall OmniScientist-V2
                    if ($LASTEXITCODE -ne 0) {
                        Write-Warning "Could not remove $($row.Path) (uv exit=$LASTEXITCODE)."
                        $script:MigrationCleanupFailed = $true
                    }
                } finally {
                    if ($null -eq $previousHome) { Remove-Item Env:UV_TOOL_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_DIR = $previousHome }
                    if ($null -eq $previousBin) { Remove-Item Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_BIN_DIR = $previousBin }
                    if ($temporaryBin) { Remove-Item -Recurse -Force $temporaryBin -ErrorAction SilentlyContinue }
                }
            } elseif (Test-Command "uv") {
                & uv pip uninstall --python $row.Python OmniScientist-V2
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "Could not remove $($row.Path) (uv exit=$LASTEXITCODE)."
                    $script:MigrationCleanupFailed = $true
                }
            } else {
                & $row.Python -m pip uninstall -y OmniScientist-V2
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "Could not remove $($row.Path) (pip exit=$LASTEXITCODE)."
                    $script:MigrationCleanupFailed = $true
                }
            }
        } else {
            Write-Warning "Could not identify the owner of $($row.Path); it was left in place."
            $script:MigrationCleanupFailed = $true
        }
    }
    if ($MigrationCleanupFailed) {
        throw "Migration is incomplete: at least one previous Omni installation remains. Resolve the warnings and rerun the installer; ownership metadata was not changed."
    }
}

try {
Acquire-InstallLock
Wait-PreviousUninstall
Write-Host "-> Checking existing Omni installations"
$existing = @(Get-OmniInstallations)
Resolve-DuplicateInstallations $existing

switch ($Method) {
    "uv" { Install-UvTool }
    "env" {
        $python = if ($EnvPythonOverride) { $EnvPythonOverride } else { Get-ActivePython }
        if (-not $python) { Write-Error "-Method env requires an explicitly active venv/conda environment."; exit 1 }
        Install-IntoEnv $python
    }
}

Write-Host ""
if (-not (Test-Path $InstalledOmni)) {
    Write-Error "Installation completed, but the expected launcher was not found: $InstalledOmni"
    exit 1
}
Write-Host "OK installed: $InstalledOmni"
& $InstalledOmni --version
Assert-NativeSuccess "Verifying the installed OmniScientist launcher"
Remove-PreviousInstallations $existing
# Record the update "channel" so `omni update` can read intent explicitly.
$recordChannel = $Channel
if (-not $recordChannel) {
    switch ($SourceMode) {
        "pypi" { $recordChannel = "pypi" }
        "git" { $recordChannel = if ($TrackBranch) { $Ref } else { "pinned" } }
        "local" { $recordChannel = if ($Editable) { "editable" } else { "local" } }
    }
}
$recordArgs = @("_record-install", "--method", $InstalledMethod, "--source", $Spec)
if ($Editable) { $recordArgs += "--editable" }
if ($recordChannel) { $recordArgs += @("--channel", $recordChannel) }
try {
    & $InstalledOmni @recordArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Installation ownership metadata could not be recorded (exit=$LASTEXITCODE)."
    }
} catch {
    Write-Warning "Installation ownership metadata could not be recorded: $_"
}
$failureRecord = Join-Path (Get-InstallStateDir) "uninstall.failed"
Remove-Item -LiteralPath $failureRecord -Force -ErrorAction SilentlyContinue

Write-Host "-> Converging bundled runtimes and Home Service"
& $InstalledOmni _converge-install
if ($LASTEXITCODE -ne 0) {
    Write-Error "OmniScientist lifecycle convergence failed (exit=$LASTEXITCODE)."
    exit $LASTEXITCODE
}

Write-Host "First-time setup: omni init"
Write-Host 'Or run: omni   (the first bare `omni` launch opens the same setup wizard automatically)'
if ($TrackBranch) {
    Write-Host "Update (latest master): omni update   # re-resolves the branch tip each run"
} elseif ($SourceMode -eq "local") {
    Write-Host "Redeploy this checkout (incl. uncommitted): cli\scripts\install.ps1"
    if ($Editable) { Write-Host "  editable install: pure-Python edits are live on next launch; rerun the installer to re-sync dependencies" }
    Write-Host "Pull + reinstall from git: omni update"
} else {
    Write-Host "Later updates: omni update"
}
Write-Host "Installation diagnostics: omni doctor"
Write-Host "Uninstall preview: omni uninstall --dry-run"
} finally {
    Release-InstallLock
}
