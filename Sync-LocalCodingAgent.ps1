# Safe, one-command Git sync for LocalCodingAgent.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\Sync-LocalCodingAgent.ps1
# To include new source files: .\Sync-LocalCodingAgent.ps1 -AddFiles @('tests/test_new_feature.py')
[CmdletBinding()]
param(
    [string]$Repo = 'D:\Workspaces\LocalCodingAgent',
    [string[]]$AddFiles = @(),
    [string]$Message = 'Checkpoint LocalCodingAgent changes'
)
$ErrorActionPreference = 'Stop'
$ExpectedRemote = 'https://github.com/clamchop-1/new-project-agent-work.git'
function Stop-Sync([string]$Reason) { throw "SYNC STOPPED: $Reason" }
function Invoke-GitChecked([string[]]$GitArgs) {
    & git.exe @GitArgs
    if ($LASTEXITCODE -ne 0) { Stop-Sync "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)" }
}
try {
    if (-not (Test-Path -LiteralPath $Repo -PathType Container)) { Stop-Sync "Repository directory not found: $Repo" }
    Set-Location -LiteralPath $Repo
    $top = (& git.exe rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0) { Stop-Sync 'Not a Git repository' }
    $expectedTop = (Resolve-Path -LiteralPath $Repo).Path.TrimEnd('\','/')
    if ([IO.Path]::GetFullPath($top).TrimEnd('\','/') -ine $expectedTop) { Stop-Sync "Unexpected Git root: $top" }
    $branch = (& git.exe branch --show-current).Trim()
    if ($branch -ne 'main') { Stop-Sync "Expected main branch; found '$branch'" }
    $remote = (& git.exe remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $remote -ne $ExpectedRemote) { Stop-Sync "Unexpected origin URL: $remote" }
    $staged = & git.exe diff --cached --name-only
    if ($LASTEXITCODE -ne 0) { Stop-Sync 'Could not inspect staging area' }
    if ($staged) { Stop-Sync "Files already staged; review manually first: $($staged -join ', ')" }

    Write-Host '[1/6] Fetching origin/main...' -ForegroundColor Cyan
    Invoke-GitChecked @('fetch','origin','main')
    $counts = (& git.exe rev-list --left-right --count 'HEAD...origin/main')
    if ($LASTEXITCODE -ne 0) { Stop-Sync 'Cannot compare local and remote history' }
    $parts = ($counts.Trim() -split '\s+')
    $ahead = [int]$parts[0]; $behind = [int]$parts[1]
    if ($behind -gt 0) { Stop-Sync "Remote is $behind commit(s) ahead (local ahead: $ahead). Resolve manually; no auto-merge or force-push." }

    Write-Host '[2/6] Running full Python test suite...' -ForegroundColor Cyan
    & py -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { Stop-Sync 'Tests failed; nothing staged, committed or pushed' }

    Write-Host '[3/6] Staging tracked changes...' -ForegroundColor Cyan
    Invoke-GitChecked @('add','-u','--','.')
    foreach ($file in $AddFiles) {
        if ([string]::IsNullOrWhiteSpace($file)) { Stop-Sync 'Empty AddFiles entry' }
        $relative = $file.Replace('\','/')
        if ($relative.StartsWith('/') -or $relative -match '(^|/)\.\.(/|$)' -or $relative -match '^[a-zA-Z]:' -or $relative -match '(^|/)(\.git|\.local)(/|$)' -or $relative -match '(?i)(\.bak$|\.env$|\.key$|\.pem$|\.pfx$|\.sqlite$|\.db$)') {
            Stop-Sync "Unsafe new-file path: $file"
        }
        $full = [IO.Path]::GetFullPath((Join-Path $expectedTop $relative))
        if (-not $full.StartsWith($expectedTop + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { Stop-Sync "Outside repository: $file" }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { Stop-Sync "File missing: $file" }
        if ((Get-Item -LiteralPath $full).Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Sync "Symlink/reparse point not allowed: $file" }
        Invoke-GitChecked @('add','--',$relative)
    }
    Invoke-GitChecked @('diff','--cached','--check')
    $names = & git.exe diff --cached --name-only
    if ($LASTEXITCODE -ne 0) { Stop-Sync 'Cannot inspect staged paths' }
    foreach ($name in $names) {
        if ($name -match '(?i)(^|/)(\.local|\.git)/|\.bak$|(^|/)\.env$|\.(key|pem|pfx)$') {
            Invoke-GitChecked @('reset','--')
            Stop-Sync "Blocked staged sensitive/runtime path: $name"
        }
    }
    Write-Host '[4/6] Reviewing staged summary...' -ForegroundColor Cyan
    Invoke-GitChecked @('diff','--cached','--stat')
    if ($names) {
        Write-Host "Staged: $($names -join ', ')"
        $confirmation = Read-Host 'Commit and push these files? Type YES to continue'
        if ($confirmation -cne 'YES') { Invoke-GitChecked @('reset','--'); Stop-Sync 'User declined; staged changes cleared' }
        Invoke-GitChecked @('commit','-m',$Message)
    } else {
        Write-Host 'No new tracked changes to commit.'
    }

    Write-Host '[5/6] Pushing main (never force)...' -ForegroundColor Cyan
    Invoke-GitChecked @('push','-u','origin','main')
    Write-Host '[6/6] Verifying synchronization...' -ForegroundColor Cyan
    Invoke-GitChecked @('fetch','origin','main')
    $localHead = (& git.exe rev-parse HEAD).Trim()
    $remoteHead = (& git.exe rev-parse origin/main).Trim()
    if ($localHead -ne $remoteHead) { Stop-Sync "Local HEAD and origin/main differ ($localHead vs $remoteHead)" }
    Write-Host "SUCCESS: main synced to origin/main at $localHead" -ForegroundColor Green
    Write-Host 'Untracked backup files were not staged. Existing ignored files were not uploaded.'
    Invoke-GitChecked @('status','-sb')
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
