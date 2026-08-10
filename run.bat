@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto check_env

echo ==============================================
echo  Primera ejecucion: instalando la aplicacion
echo  (esto solo pasa una vez, puede tardar varios
echo  minutos segun tu internet)
echo ==============================================
echo.

py -3.12 -m venv .venv
if errorlevel 1 goto no_python

echo Instalando dependencias...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto pip_fail

where nvidia-smi >nul 2>&1
if errorlevel 1 goto check_env
echo GPU NVIDIA detectada: instalando librerias CUDA...
.venv\Scripts\python.exe -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

:check_env
if exist ".env" goto run
echo.
echo Falta tu clave de Gemini (es gratis):
echo   1. Entra a https://aistudio.google.com y crea una API key.
echo   2. Pegala aqui abajo y presiona Enter.
echo.
set /p GEMINI_KEY=GEMINI_API_KEY:
if "%GEMINI_KEY%"=="" goto check_env
echo GEMINI_API_KEY=%GEMINI_KEY%> .env

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1" "%~dp0" >nul 2>&1
start "" ".venv\Scripts\pythonw.exe" -m src.chat_app
exit /b

:no_python
echo.
echo ERROR: No se encontro Python 3.12 en esta computadora.
echo Para instalarlo, abre una terminal (tecla Windows, escribe "cmd",
echo Enter) y ejecuta:
echo.
echo     winget install Python.Python.3.12
echo.
echo Cuando termine, vuelve a hacer doble clic en run.bat
pause
exit /b 1

:pip_fail
echo.
echo ERROR: Fallo la instalacion de dependencias.
echo Revisa tu conexion a internet y vuelve a ejecutar run.bat
pause
exit /b 1
