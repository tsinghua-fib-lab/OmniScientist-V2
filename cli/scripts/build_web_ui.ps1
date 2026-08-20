<# Build the loopback SPA into web/dist for Windows source deployments. #>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WebRoot = Join-Path $Root "web"
$PackageJson = Join-Path $WebRoot "package.json"
$Dist = Join-Path $WebRoot "dist"
$Stage = Join-Path $WebRoot ".omni-web-dist-$PID-$([guid]::NewGuid().ToString('N'))"
$Backup = ""

if (-not (Test-Path $PackageJson)) {
    throw "web/package.json missing; cannot build the omni web UI"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js is required to package the omni web UI"
}

New-Item -ItemType Directory -Path $Stage | Out-Null
Push-Location $WebRoot
try {
    if (Get-Command pnpm -ErrorAction SilentlyContinue) {
        & pnpm install --frozen-lockfile
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed (exit=$LASTEXITCODE)" }
        & pnpm exec vite build --outDir $Stage
        if ($LASTEXITCODE -ne 0) { throw "pnpm build failed (exit=$LASTEXITCODE)" }
    } elseif (Get-Command npm -ErrorAction SilentlyContinue) {
        & npm install --no-package-lock
        if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit=$LASTEXITCODE)" }
        & npm exec -- vite build --outDir $Stage
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit=$LASTEXITCODE)" }
    } else {
        throw "pnpm or npm is required to package the omni web UI"
    }

    if (-not (Test-Path (Join-Path $Stage "index.html"))) {
        throw "web/dist/index.html was not produced"
    }
    $InitText = Get-Content (Join-Path $Root "cli\src\omni\__init__.py") -Raw
    $Match = [regex]::Match($InitText, '__version__\s*=\s*["'']([^"'']+)["'']')
    if (-not $Match.Success) {
        throw "Could not read OmniScientist version from cli/src/omni/__init__.py"
    }
    $Json = @{ version = $Match.Groups[1].Value } | ConvertTo-Json
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $Stage "version.json"),
        "$Json`n",
        $Utf8NoBom
    )
    Write-Host "stamped UI $($Match.Groups[1].Value)"

    if (Test-Path $Dist) {
        $Backup = Join-Path $WebRoot ".omni-web-backup-$PID-$([guid]::NewGuid().ToString('N'))"
        Move-Item -LiteralPath $Dist -Destination $Backup
    }
    Move-Item -LiteralPath $Stage -Destination $Dist
    $Stage = ""
    if ($Backup) {
        Remove-Item -LiteralPath $Backup -Recurse -Force
        $Backup = ""
    }
    Write-Host "built $Dist"
} finally {
    Pop-Location
    if ($Stage -and (Test-Path $Stage)) {
        Remove-Item -LiteralPath $Stage -Recurse -Force
    }
    if ($Backup -and (Test-Path $Backup)) {
        if (-not (Test-Path $Dist)) {
            Move-Item -LiteralPath $Backup -Destination $Dist
        } else {
            Remove-Item -LiteralPath $Backup -Recurse -Force
        }
    }
}
