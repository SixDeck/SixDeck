# Build standalone SixDeck.exe (Windows x64)
$ErrorActionPreference = "Stop"

$pyi = if (Test-Path ".\.venv\Scripts\pyinstaller.exe") { ".\.venv\Scripts\pyinstaller.exe" } else { "pyinstaller" }

& $pyi --clean --noconfirm --onefile --windowed `
    --name SixDeck `
    --icon Assets\launcher_icon.ico `
    --add-data "launcher_ui.html;." `
    --add-data "Assets;Assets" `
    --add-data "config.yaml;." `
    launcher_app.py

if ($LASTEXITCODE -eq 0) {
    Copy-Item -Force "dist\SixDeck.exe" "SixDeck.exe"
    Write-Host "Build complete: SixDeck.exe" -ForegroundColor Green
}
