@echo off
setlocal
cd /d "%~dp0"
set "local_python=%~dp0.venv\Scripts\python.exe"
if not exist "%local_python%" (
  echo LocalCodingAgent's verified Python environment is missing.
  exit /b 2
)
"%local_python%" -B "%~dp0agent.py" local-chat --provider ssh_tunnel_ollama %*
exit /b %ERRORLEVEL%
