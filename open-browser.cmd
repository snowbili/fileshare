@echo off
rem  Open the UI in the DEFAULT browser instead of the app window.
rem  Keep this file ASCII-only (see the note in run.cmd).
rem  Reuses run.cmd so the Python detection lives in one place:
rem  run.cmd adds --open, this file adds --browser, so server.py
rem  prefers the default browser.
call "%~dp0run.cmd" --browser %*
