@echo off
rem ============================================================
rem  Allow FileShare through Windows Firewall (run as Administrator).
rem  Rules are limited to the PRIVATE profile on purpose.
rem  Keep this file ASCII-only (see the note in run.cmd).
rem ============================================================
set "PORT=8099"
if not "%~1"=="" set "PORT=%~1"

net session >nul 2>nul
if errorlevel 1 (
  echo [Administrator rights required] Right-click this file and choose "Run as administrator".
  echo.
  echo Or run these two commands in an elevated PowerShell / CMD:
  echo   netsh advfirewall firewall add rule name="FileShare TCP %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=private
  echo   netsh advfirewall firewall add rule name="FileShare UDP 53317" dir=in action=allow protocol=UDP localport=53317 profile=private
  pause
  exit /b 1
)

echo Adding firewall rules (TCP %PORT% and UDP 53317, private profile only) ...
netsh advfirewall firewall delete rule name="FileShare TCP %PORT%" >nul 2>nul
netsh advfirewall firewall delete rule name="FileShare UDP 53317" >nul 2>nul
netsh advfirewall firewall add rule name="FileShare TCP %PORT%" dir=in action=allow protocol=TCP localport=%PORT% profile=private
netsh advfirewall firewall add rule name="FileShare UDP 53317" dir=in action=allow protocol=UDP localport=53317 profile=private

echo.
echo Done. Rule names: "FileShare TCP %PORT%" and "FileShare UDP 53317".
echo To remove them later:
echo   netsh advfirewall firewall delete rule name="FileShare TCP %PORT%"
echo   netsh advfirewall firewall delete rule name="FileShare UDP 53317"
pause

