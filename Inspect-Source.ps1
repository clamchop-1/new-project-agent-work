param([Parameter(Mandatory=$true)][string]$LiteralFile)
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $LiteralFile
[pscustomobject]@{Name=$item.Name;Bytes=$item.Length;SHA256=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash} | ConvertTo-Json
