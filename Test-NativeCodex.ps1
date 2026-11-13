# Run manually in a NORMAL PowerShell window, outside Codex's command sandbox.
# No model inference, installation, admin setup, global config or policy change.
$ErrorActionPreference = 'Stop'
$probeRoot = Join-Path $PSScriptRoot '.local\probes'
$fixture = Join-Path $probeRoot 'fixture'
$probeHome = Join-Path $probeRoot 'mxc\home'
if (-not (Test-Path -LiteralPath (Join-Path $probeHome 'config.toml') -PathType Leaf)) {
    throw 'Prepared isolated MXC probe is missing. Do not substitute your global configuration.'
}
$previousCodexRoot = $env:CODEX_HOME
$previousHomeValue = $env:HOME
try {
    $env:CODEX_HOME = $probeHome
    $env:HOME = $probeHome
    Write-Host 'Testing native MXC command isolation only; no model request is made.'
    & codex -c 'windows.sandbox="mxc"' sandbox --include-managed-config `
        --permission-profile repo_read --cd $fixture -- cmd.exe /d /c echo MXC_OK
    $probeExit = $LASTEXITCODE
    if ($probeExit -eq 0) {
        Write-Host 'PASS: MXC command startup works in this Windows context.'
        Write-Host 'Next: run the filesystem/network sentinel tests before enabling agent commands.'
    } else {
        Write-Host "NOT READY: native MXC check returned $probeExit. Preserve this output for diagnosis."
        Write-Host 'No elevated/unelevated/full-access fallback was attempted.'
    }
} finally {
    $env:CODEX_HOME = $previousCodexRoot
    $env:HOME = $previousHomeValue
}
