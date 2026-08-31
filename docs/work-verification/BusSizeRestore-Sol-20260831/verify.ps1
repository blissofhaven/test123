param([ValidateSet('full','native','baseline','docs')][string]$Run='full')
$ErrorActionPreference='Stop'
$project='C:\Users\shock\OneDrive\Desktop\Клауд\rza-calc-0.3-safe-hardening'
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:QT_QPA_PLATFORM=if($Run -eq 'native'){'windows'}else{'offscreen'}
Push-Location -LiteralPath $project
try {
 switch($Run) {
  'full' { python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider --durations=5 "--junitxml=$PSScriptRoot\pytest-final.xml" 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'pytest-final.txt') }
  'native' { python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider tests/test_bus_band_geometry.py tests/test_ui_line_direction.py tests/test_visio_style_integration.py tests/test_ui_connection_cleanup_gap.py tests/test_ui_connection_cleanup.py tests/test_bus_connection_spacing.py tests/test_ui_direct_connections.py tests/test_ui_connected_drag.py tests/test_rotation_handle.py tests/test_ui_b3_label_geometry.py 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'native-final.txt') }
  'baseline' { python -B -X utf8 tools_update_baseline.py --stage BusSizeRestore --reason 'Возврат прежней толщины шин по запросу заказчика' --evidence docs/work-verification/bus-size-restore-report.md 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'baseline-final.txt') }
  'docs' { python -B -X utf8 -m pytest -q --capture=sys -p no:cacheprovider tests/test_roadmap_consistency.py 2>&1 | Tee-Object -FilePath (Join-Path $PSScriptRoot 'roadmap-final.txt') }
 }
 $result=$LASTEXITCODE
} finally { Pop-Location }
exit $result
