@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python не найден.
  echo Установите его с python.org и обязательно отметьте галочку
  echo "Add python.exe to PATH" на первом экране установщика.
  echo.
  pause
  exit /b 2
)

echo Текстовый отчёт по учебной схеме нефтепромысла "Таёжный" с ГТЭС.
echo Окно программы открывается файлом ЗАПУСК.bat
echo.
python -m rza_calc rza_calc\examples\oilfield_gtes.json report
echo.
pause
