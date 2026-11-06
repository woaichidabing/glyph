# 发布 Windows 安装包：先检查版本，再构建
# 用法：.\scripts\release.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\web-desktop2")

$version = (Get-Content package.json -Raw | ConvertFrom-Json).version
Write-Host ("发布版本: " + $version) -ForegroundColor Cyan

if (git tag --list ("v" + $version)) {
  throw ("版本 v" + $version + " 的标签已经存在。要发新版本，先改 package.json 的 version（npm version patch/minor/major）")
}

Write-Host "构建安装包..." -ForegroundColor Cyan
npm run build
npx electron-builder --win nsis
if ($LASTEXITCODE -ne 0) { throw "electron-builder failed" }

Get-ChildItem release -Filter "Glyph-Setup-*.exe" | ForEach-Object {
  Write-Host ("产物: " + $_.FullName + "  " + [math]::Round($_.Length/1MB,1) + " MB") -ForegroundColor Green
}
