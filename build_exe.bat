@echo off
setlocal

rem Builds the standalone CallAssist.exe (onedir) into dist\CallAssist\.
rem Run from a fresh clone: creates its own venv if none exists yet, then
rem installs runtime + dev (PyInstaller) requirements before building.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating a virtual environment in .venv ...
    py -3.12 -m venv .venv || python -m venv .venv || goto :no_python
)

set PY=.venv\Scripts\python.exe

echo Installing requirements...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
"%PY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt || goto :fail

echo Building CallAssist.exe (onedir, this can take a few minutes)...
"%PY%" -m PyInstaller CallAssist.spec --noconfirm || goto :fail

echo.
echo Done. dist\CallAssist\CallAssist.exe
exit /b 0

:no_python
echo Could not find a Python launcher (py or python) on PATH.
exit /b 1

:fail
echo.
echo Build failed - see the errors above.
exit /b 1
