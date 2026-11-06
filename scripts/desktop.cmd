@echo off
rem Glyph desktop launcher (ASCII only: cmd reads this file as the system codepage)
rem Some editors/tools inject ELECTRON_RUN_AS_NODE=1; clear it so Electron runs as an app.
set ELECTRON_RUN_AS_NODE=
cd /d "%~dp0..\web-desktop2"
if not exist "node_modules\electron\dist\electron.exe" (
  echo Electron is not installed. Run: npm install
  pause
  exit /b 1
)
"node_modules\electron\dist\electron.exe" electron\main.cjs
