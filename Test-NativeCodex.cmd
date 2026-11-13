@echo off
setlocal
rem Read-only native sandbox startup check. No model inference or installation.
set "CODEX_HOME=%~dp0.local\probes\mxc\home"
set "HOME=%CODEX_HOME%"
if not exist "%CODEX_HOME%\config.toml" (
  echo Prepared isolated probe configuration is missing.
  exit /b 2
)
codex -c windows.sandbox=mxc sandbox --include-managed-config --permission-profile repo_read --cd "%~dp0.local\probes\fixture" -- cmd.exe /d /c echo MXC_OK
set "probe_result=%ERRORLEVEL%"
if "%probe_result%"=="0" (
  echo PASS: MXC command startup works in this Windows context.
  echo Filesystem and network sentinel tests are still required before agent execution.
) else (
  echo NOT READY: preserve the error above. No permissive fallback was attempted.
)
exit /b %probe_result%
