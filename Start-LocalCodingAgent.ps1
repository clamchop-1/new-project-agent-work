param([string]$Project='KronosStage',
      [string]$Task='Perform a read-only takeover. Explain the architecture, current hold/task and next safe step.',
      [string]$Model)
$ErrorActionPreference='Stop'
$arguments = @('--project', $Project, '--task', $Task)
if ($Model) { $arguments += @('--model', $Model) }
& (Join-Path $PSScriptRoot 'Start-LocalCodingAgent.cmd') @arguments
exit $LASTEXITCODE
