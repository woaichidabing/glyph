# 把后端打成单文件 exe（Windows）
# 产物：dist\glyph-backend.exe
# 用法：.\scripts\build-backend.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = ".venv\Scripts\python.exe"
Write-Host "1/3 检查 PyInstaller..." -ForegroundColor Cyan
& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "   没装，正在安装..." -ForegroundColor Yellow
  & $py -m pip install "pyinstaller>=6.6" | Out-Null
}

Write-Host "2/3 打包（第一次会慢，2-5 分钟）..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --onefile --name glyph-backend `
  --paths src `
  --collect-submodules uvicorn `
  --collect-submodules fastapi `
  --collect-submodules pydantic `
  --hidden-import web.server `
  --add-data "data/dict.db;glyph/_data" `
  --add-data "src/glyph/_data;glyph/_data" `
  backend_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "3/3 产物：" -ForegroundColor Cyan
$exe = Get-Item dist\glyph-backend.exe
$mb = [math]::Round($exe.Length/1MB, 1)
Write-Host ("   " + $exe.FullName + "  " + $mb + " MB")
