param(
    [string]$Version = "1.1.0"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildRoot = Join-Path $Root "build\windows-release"
$PyInstallerDist = Join-Path $BuildRoot "pyinstaller-dist"
$PyInstallerWork = Join-Path $BuildRoot "pyinstaller-work"
$StageName = "SciDataFusion-$Version-windows-x64"
$Stage = Join-Path $BuildRoot $StageName
$Dist = Join-Path $Root "dist"
$Archive = Join-Path $Dist "$StageName.zip"
$Checksum = "$Archive.sha256"

if (-not $BuildRoot.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Build directory must remain inside the repository"
}
Remove-Item -LiteralPath $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Checksum -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $BuildRoot, $Dist -Force | Out-Null

Push-Location $Root
try {
    & uv run pyinstaller `
        --noconfirm `
        --clean `
        --onedir `
        --console `
        --name SciDataFusion `
        --distpath $PyInstallerDist `
        --workpath $PyInstallerWork `
        --specpath $BuildRoot `
        --additional-hooks-dir packaging\pyinstaller_hooks `
        --collect-data scidatafusion `
        --hidden-import astropy.io.fits `
        --exclude-module astropy.visualization `
        --exclude-module matplotlib `
        --exclude-module pytest `
        --exclude-module hypothesis `
        --copy-metadata fastapi `
        --copy-metadata pydantic `
        --copy-metadata pydantic-settings `
        --copy-metadata uvicorn `
        src\scidatafusion\desktop.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    Copy-Item -LiteralPath (Join-Path $PyInstallerDist "SciDataFusion") -Destination $Stage -Recurse
    Copy-Item -LiteralPath ".env.example" -Destination (Join-Path $Stage ".env.example")
    Copy-Item -LiteralPath "prompts" -Destination (Join-Path $Stage "prompts") -Recurse
    Copy-Item -LiteralPath "packaging\windows\Start-SciDataFusion.bat" -Destination $Stage
    Copy-Item -LiteralPath "packaging\windows\README-Windows.txt" -Destination $Stage

    if (Test-Path (Join-Path $Stage ".env")) {
        throw "Release stage must not contain a local .env file"
    }
    Compress-Archive -LiteralPath $Stage -DestinationPath $Archive -CompressionLevel Optimal
    $Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $StageName.zip" | Set-Content -LiteralPath $Checksum -Encoding ascii
    Write-Host "Built $Archive"
    Write-Host "SHA256 $Hash"
}
finally {
    Pop-Location
}
