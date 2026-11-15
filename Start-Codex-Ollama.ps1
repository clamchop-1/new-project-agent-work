param(
    [string]$ProjectPath = (Get-Location).Path,
    [string]$Model = 'qwen3.5:4b-codex-test',
    [ValidateRange(2048, 262144)]
    [int]$ContextWindow = 8192,
    [switch]$Preview,
    [switch]$TrustProject
)

$ErrorActionPreference = 'Stop'

function ConvertTo-TomlString([string]$Value) {
    # JSON string escaping is compatible with TOML basic strings.
    return ConvertTo-Json -InputObject $Value -Compress
}

if ($Model -notmatch '^[A-Za-z0-9._/:-]+$') {
    throw 'Model names may contain only letters, numbers, dots, underscores, slashes, colons, and hyphens.'
}

$project = (Resolve-Path -LiteralPath $ProjectPath).Path
if (-not (Test-Path -LiteralPath $project -PathType Container)) {
    throw "Project path is not a directory: $project"
}

$codexCommand = Get-Command 'codex' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $codexCommand) {
    throw 'Codex CLI was not found on PATH. No installation was attempted.'
}

$profileRoot = Join-Path $PSScriptRoot '.local\co'
do {
    $profileName = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $profile = Join-Path $profileRoot $profileName
} while (Test-Path -LiteralPath $profile)
New-Item -ItemType Directory -Path $profile -Force | Out-Null

$catalog = @{
    models = @(@{
        slug = $Model
        display_name = $Model
        context_window = $ContextWindow
        shell_type = 'default'
        visibility = 'list'
        supported_in_api = $true
        priority = 0
        truncation_policy = @{ mode = 'bytes'; limit = 10000 }
        input_modalities = @('text')
        base_instructions = ''
        support_verbosity = $true
        default_verbosity = 'low'
        supports_parallel_tool_calls = $false
        supports_reasoning_summaries = $false
        supported_reasoning_levels = @()
        default_reasoning_level = $null
        experimental_supported_tools = @()
    })
}
$catalogPath = Join-Path $profile 'model.json'
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($catalogPath, ($catalog | ConvertTo-Json -Depth 8), $utf8WithoutBom)
$modelToml = ConvertTo-TomlString $Model
$catalogPathToml = ConvertTo-TomlString ($catalogPath -replace '\\', '/')
$projectToml = ConvertTo-TomlString ($project -replace '\\', '/')
$projectTrustConfig = if ($TrustProject) {
    "[projects.$projectToml]`ntrust_level = `"trusted`"`n"
} else {
    ''
}

$config = @"
model = $modelToml
model_provider = "local_ollama"
model_catalog_json = $catalogPathToml
model_context_window = $ContextWindow
model_reasoning_effort = "none"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "disabled"
$projectTrustConfig

[model_providers.local_ollama]
name = "Only local Ollama"
base_url = "http://127.0.0.1:11434/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.local_ollama.capabilities]
remote_compaction = "unsupported"

[analytics]
enabled = false

[windows]
sandbox = "mxc"
"@
$configPath = Join-Path $profile 'config.toml'
[System.IO.File]::WriteAllText($configPath, $config, $utf8WithoutBom)

Write-Host "Project: $project"
Write-Host "Model: $Model (context window $ContextWindow)"
Write-Host "Isolated Codex profile: $profile"
Write-Host 'Provider: http://127.0.0.1:11434/v1 (Responses API)'
Write-Host 'Approval: on-request; sandbox: workspace-write; web search: disabled'
if ($TrustProject) { Write-Host 'Project trust: enabled for this isolated profile only' }
if ($Preview) {
    Write-Host "Preview only. Configuration: $configPath"
    exit 0
}

$previousCodexHome = $env:CODEX_HOME
$previousHome = $env:HOME
$previousNoProxy = $env:NO_PROXY
$removedVariables = @{}
try {
    $env:CODEX_HOME = $profile
    $env:HOME = $profile
    $env:NO_PROXY = '127.0.0.1,localhost'
    foreach ($item in Get-ChildItem Env:) {
        if ($item.Name -match '(?i)(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ACCESS_KEY|PRIVATE_KEY|KEYFILE|CERTIFICATE)' -or
            $item.Name -match '^(?i:GOOGLE_APPLICATION_CREDENTIALS|GOOGLE_CLOUD_KEYFILE_JSON)$') {
            $removedVariables[$item.Name] = $item.Value
            Remove-Item -LiteralPath "Env:$($item.Name)"
        }
    }
    & $codexCommand.Source --no-daemon --cd $project
    exit $LASTEXITCODE
}
finally {
    foreach ($name in $removedVariables.Keys) {
        Set-Item -LiteralPath "Env:$name" -Value $removedVariables[$name]
    }
    $env:CODEX_HOME = $previousCodexHome
    $env:HOME = $previousHome
    $env:NO_PROXY = $previousNoProxy
}
