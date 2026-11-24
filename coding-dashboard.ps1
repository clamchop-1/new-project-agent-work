param(
    [Parameter(Mandatory=$true)][string[]]$DashboardRoot,
    [Parameter(Mandatory=$true)][string[]]$CodingPath,
    [ValidateSet('clamp_percent_v1','stats_multi_module_v1')][string]$TestProfile = 'clamp_percent_v1',
    [ValidateSet('configured','local_only','remote_allowed')][string]$RoutingMode = 'configured',
    [string]$Provider,
    [int]$Port = 52718
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "The existing Python environment was not found at $python."
}

$arguments = @((Join-Path $projectRoot 'agent.py'), 'coding-dashboard',
    '--dashboard-port', [string]$Port, '--coding-test-profile', $TestProfile,
    '--routing-mode', $RoutingMode)
foreach ($root in $DashboardRoot) {
    $arguments += @('--dashboard-root', $root)
}
foreach ($path in $CodingPath) {
    $arguments += @('--coding-path', $path)
}
if ($Provider) {
    $arguments += @('--provider', $Provider)
}
Write-Host 'Starting the loopback-only LocalCodingAgent dashboard.'
Write-Host 'The launcher checks the configured inference provider and prints a one-run bearer token.'
Write-Host 'Keep this console open. Existing processes are never stopped automatically.'
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Dashboard stopped with exit code $LASTEXITCODE. Check the startup message above."
}
