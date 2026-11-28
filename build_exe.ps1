# Build standalone SixDeck.exe (Windows x64)
# Only bundles application code and public UI assets (never .env, data/, Mods/, or user dumps).
$ErrorActionPreference = "Stop"

$pyi = if (Test-Path ".\.venv\Scripts\pyinstaller.exe") { ".\.venv\Scripts\pyinstaller.exe" } else { "pyinstaller" }

$requiredAssets = @(
    "launcher_ui.html",
    "config.yaml",
    "Assets\BG.png",
    "Assets\launcher_icon.ico",
    "Assets\logo.png",
    "Assets\logo.svg"
)
foreach ($f in $requiredAssets) {
    if (-not (Test-Path $f)) {
        throw "Required build file missing: $f"
    }
}

& $pyi --clean --noconfirm --onefile --windowed `
    --name SixDeck `
    --icon Assets\launcher_icon.ico `
    --collect-submodules src `
    --collect-data webview `
    --add-data "launcher_ui.html;." `
    --add-data "config.yaml;." `
    --add-data "Assets\BG.png;Assets" `
    --add-data "Assets\launcher_icon.ico;Assets" `
    --add-data "Assets\logo.png;Assets" `
    --add-data "Assets\logo.svg;Assets" `
    launcher_app.py

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Copy-Item -Force "dist\SixDeck.exe" "SixDeck.exe"
$hash = (Get-FileHash -Algorithm SHA256 "SixDeck.exe").Hash.ToLowerInvariant()
"$hash  SixDeck.exe" | Set-Content -Encoding ascii "SixDeck.exe.sha256"
Write-Host "Build complete: SixDeck.exe (SHA256: $hash)" -ForegroundColor Green
