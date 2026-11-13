@echo off
setlocal
rem Manual normal-host validation only. No existing project or global setting changes.
"C:\Users\dsmus\AppData\Local\Programs\Python\Python312\python.exe" -B "%~dp0native_host_validation.py"
exit /b %ERRORLEVEL%
