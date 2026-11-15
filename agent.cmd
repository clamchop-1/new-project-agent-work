@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0interactive_agent.py" --assistant-root "%CD%\." %*
) else (
  py "%~dp0interactive_agent.py" --assistant-root "%CD%\." %*
)
