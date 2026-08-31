param([switch]$Freeze)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$project='C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening'
$before='C:\Users\shock\Documents\Codex\РЗА — резерв\2026-08-31-connection-cleanup-before-190103\before-connection-cleanup.zip'
if((Get-FileHash -LiteralPath $before).Hash.ToLowerInvariant() -ne 'd3906aa6ea9349b5c08ce8f0eaf933924331bc6d04072e17b10ccfb17373f7bb'){throw 'Checkpoint changed'}
$prior=@{}
$archive=[IO.Compression.ZipFile]::OpenRead($before)
try {
 foreach($entry in $archive.Entries){
  $stream=$entry.Open(); $digest=[Security.Cryptography.SHA256]::Create()
  try {$prior[$entry.FullName.Substring('rza-calc-0.3-safe-hardening/'.Length)]=[Convert]::ToHexString($digest.ComputeHash($stream)).ToLowerInvariant()}
  finally{$stream.Dispose();$digest.Dispose()}
 }
}finally{$archive.Dispose()}
$rows=@(Get-ChildItem -LiteralPath $project -File -Recurse -Force | Where-Object {$_.FullName -notmatch '[\\/](\.git|\.venv|\.pytest_cache|__pycache__)[\\/]' -and $_.Extension -notin @('.pyc','.pyo')} | ForEach-Object {
 [ordered]@{path=[IO.Path]::GetRelativePath($project,$_.FullName).Replace('\','/');sha256=(Get-FileHash -LiteralPath $_.FullName).Hash.ToLowerInvariant()}
})
$changed=@($rows | Where-Object {$prior.ContainsKey($_.path) -and $prior[$_.path] -ne $_.sha256} | ForEach-Object {$_.path})
$added=@($rows | Where-Object {-not $prior.ContainsKey($_.path)} | ForEach-Object {$_.path})
$missing=@($prior.Keys | Where-Object {$_ -notin $rows.path})
if($missing.Count){throw ('Input files missing: '+($missing -join ', '))}
foreach($path in $changed){
 if($path -match '^rza_calc/(core|domain|adapters|topology|calculation|io|data)/' -or $path -match '^tests/(baseline/|test_calculation_baseline.py|test_control_examples.py|baseline_snapshot.py)' -or $path -in @('tools_update_baseline.py','docs/calculation-audit/baseline-log.md')){throw "Protected file changed: $path"}
}
$result=[ordered]@{checkpoint_files=$prior.Count;current_files=$rows.Count;changed=$changed;added=$added;protected_files_unchanged=$true;missing=$missing}
$result | ConvertTo-Json -Depth 5 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'inventory.json')
if($Freeze){
 $rows | Where-Object {$_.path -like 'rza_calc/*' -or $_.path -like 'tests/*' -or $_.path -eq 'tools_update_baseline.py'} | ConvertTo-Json -Depth 4 | Out-File -LiteralPath (Join-Path $PSScriptRoot 'source-freeze.json') -Encoding utf8
}
