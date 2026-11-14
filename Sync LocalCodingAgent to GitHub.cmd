@echo off
cd /d "%~dp0"
py -3 sync_github.py %*
if errorlevel 1 pause
