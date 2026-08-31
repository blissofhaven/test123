@echo off
chcp 65001 >nul
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "PYEXE=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

"%PYEXE%" --version >nul 2>nul
if errorlevel 1 goto python_missing

"%PYEXE%" -m rza_calc.gui %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto app_failed
exit /b 0

:python_missing
echo.
echo Python не найден.
echo Установите его с python.org и на ПЕРВОМ экране установщика
echo обязательно отметьте галочку "Add python.exe to PATH".
echo.
pause
exit /b 2

:app_failed
echo.
echo РЗА-Про завершилась с кодом %EXIT_CODE%.
echo Если окно не открылось, выполните в этой папке:
echo     python -m pip install -r requirements.txt
echo.
pause
exit /b %EXIT_CODE%
