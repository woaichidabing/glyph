@echo off
rem ------------------------------------------------------------
rem Glyph dev launcher - double-click to run.
rem
rem Why this exists:
rem   the AI session cannot keep a server alive across days, so
rem   starting by hand means two terminals and two commands.
rem   This opens both in their own windows and survives on its own.
rem
rem Notes:
rem   - keep this file ASCII-only (no Chinese) so cmd never
rem     mangles it on a GBK console; paths use %~dp0, no literals.
rem   - backend: 127.0.0.1:8080   frontend: localhost:5174
rem ------------------------------------------------------------

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [X] python env missing: %CD%\.venv
  echo     run:  py -3.12 -m venv .venv
  echo     then: .venv\Scripts\python -m pip install ".[dev]" ".[qwen]" ".[grade]" ".[web]"
  pause
  exit /b 1
)

rem The project is deliberately NOT installed editable (a Chinese path makes the
rem generated .pth unreadable and crashes the interpreter), so `import glyph`
rem only works when src is on the path - same reason pytest sets pythonpath.
rem Without this the backend dies with ModuleNotFoundError: No module named 'glyph'.
set "PYTHONPATH=%CD%\src"

echo [1/3] starting backend on 127.0.0.1:8080
start "Glyph backend 8080" cmd /k ""%CD%\.venv\Scripts\python.exe" -m uvicorn web.server:app --host 127.0.0.1 --port 8080"

echo [2/3] starting desktop frontend on localhost:5174
cd /d "%~dp0..\web-desktop2"
if not exist "node_modules" (
  echo [!] node_modules missing, installing (first run takes a few minutes)
  call npm install
)
start "Glyph desktop 5174" cmd /k "npm run dev -- --strictPort"

echo [3/3] waiting for both, then opening the browser
rem ping instead of timeout.exe: Git Bash puts its own timeout first on PATH,
rem and its argument syntax breaks this line when the .bat is launched from bash.
ping -n 13 127.0.0.1 >nul 2>&1
start "" http://localhost:5174

echo.
echo     backend  http://127.0.0.1:8080
echo     desktop  http://localhost:5174
echo     close both console windows to stop everything
echo.
endlocal
