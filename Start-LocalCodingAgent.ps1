param([ValidateSet('KronosStage','PhotoLibraryTools','PhotoLibraryPlanning')][string]$Project='KronosStage',
      [string]$Task='Perform a read-only takeover. Explain the architecture, current hold/task and next safe step.',
      [string]$Model='qwen3.5:4b')
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Start-LocalCodingAgent.cmd') --project $Project --task $Task --model $Model
exit $LASTEXITCODE
