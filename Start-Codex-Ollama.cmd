@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0Start-Codex-Ollama.ps1" %*
exit /b %ERRORLEVEL%
