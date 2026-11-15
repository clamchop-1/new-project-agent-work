@echo off
setlocal
set "local_python=%~dp0.venv\Scripts\python.exe"
if not exist "%local_python%" set "local_python=C:\Users\dsmus\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%local_python%" (
  echo Verified Python interpreter is missing. No installation was attempted.
  exit /b 2
)
"%local_python%" -B "%~dp0agent.py" run %*
exit /b %ERRORLEVEL%
