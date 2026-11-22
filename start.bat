@echo off
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not defined PY (
    py -3.12 -c "import flask" >nul 2>&1 && set "PY=py -3.12"
)
if not defined PY (
    python -c "import flask" >nul 2>&1 && set "PY=python"
)

if not defined PY goto noflask

%PY% app.py %*
if errorlevel 1 pause
exit /b

:noflask
echo.
echo Flask не найден ни в .venv, ни в py 3.12, ни в python.
echo Запустите setup.bat — он создаст окружение и поставит зависимости.
echo.
pause
