@echo off
rem ============================================================
rem  FileShare launcher.
rem  NOTE: keep this file ASCII-only. cmd.exe reads batch files by
rem  byte offset; non-ASCII text plus "chcp 65001" makes it resume
rem  mid-line and execute garbage. Chinese docs live in README.md.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---- pick an available Python: 3.14 -> 3.8, then python / python3 ----
set "PY="
for %%V in (3.14 3.13 3.12 3.11 3.10 3.9 3.8) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -%%V"
  )
)
if not defined PY ( where python  >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY (
  echo [ERROR] Python 3.8+ not found.
  echo         Install from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
  exit /b 1
)

rem ---- double check the version is >= 3.8 ----
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,8) else 7)" >nul 2>nul
if errorlevel 7 (
  echo [ERROR] The detected Python is older than 3.8, please upgrade.
  %PY% -V
  pause
  exit /b 1
)

echo Starting FileShare (LAN file transfer) ...
echo Using interpreter: %PY%
if not exist "data" mkdir "data"
if not exist "shared" mkdir "shared"
if not exist "inbox" mkdir "inbox"
if not exist "downloads" mkdir "downloads"

%PY% "app\server.py" --open %*
if errorlevel 2 pause
endlocal


