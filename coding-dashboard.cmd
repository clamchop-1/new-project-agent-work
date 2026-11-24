@echo off
setlocal
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo LocalCodingAgent Python environment was not found at "%PYTHON%".
  exit /b 2
)
"%PYTHON%" -u -B "%~dp0agent.py" coding-dashboard %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo Dashboard stopped with exit code %RESULT%. Existing processes were left untouched.
exit /b %RESULT%
