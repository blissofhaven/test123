param([ValidateSet('full','baseline','native','docs')][string]$Run='full',[string]$Label='input')
$ErrorActionPreference='Stop'
$project='C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening'
$evidence='C:\Users\shock\Documents\Codex\2026-08-30\c-users-shock-onedrive-desktop-rza\work\connection-cleanup-20260831'
Set-Location -LiteralPath $project
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:QT_QPA_PLATFORM=if($Run -eq 'native'){'windows'}else{'offscreen'}
switch($Run){
 'full' {python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider --durations=8 "--junitxml=$evidence\pytest-$Label.xml" 2>&1 | Tee-Object -FilePath (Join-Path $evidence "pytest-$Label.txt")}
 'baseline' {python -B -X utf8 tools_update_baseline.py --stage ConnectionCleanup --reason 'Проверка согласованных исправлений редактора' --evidence 'docs/work-verification/connection-cleanup-report.md' 2>&1 | Tee-Object -FilePath (Join-Path $evidence "baseline-$Label.txt")}
 'docs' {python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider tests/test_roadmap_consistency.py 2>&1 | Tee-Object -FilePath (Join-Path $evidence "roadmap-$Label.txt")}
 'native' {python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider tests/test_ui_connection_cleanup.py tests/test_ui_connection_cleanup_inspector.py tests/test_ui_connection_cleanup_gap.py tests/test_legacy_ct_reconnection.py tests/test_legacy_connection_voltage.py tests/test_editor_deletion_cleanup.py tests/test_bus_connection_spacing.py tests/test_ui_direct_connections.py tests/test_voltage_inspector.py tests/test_ui_connected_commands.py tests/test_ui_connected_drag.py tests/test_stage4_saved_route_editing.py 2>&1 | Tee-Object -FilePath (Join-Path $evidence "native-$Label.txt")}
}
exit $LASTEXITCODE
