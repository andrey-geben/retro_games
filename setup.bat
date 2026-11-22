@echo off
cd /d "%~dp0"
echo Создаю виртуальное окружение .venv ...
py -3.12 -m venv .venv
if errorlevel 1 python -m venv .venv
if errorlevel 1 goto fail

call ".venv\Scripts\activate.bat"
echo Обновляю pip ...
python -m pip install --upgrade pip
echo Ставлю зависимости ...
python -m pip install -r requirements.txt
python -m pip install pyinstaller

echo.
echo Готово. Теперь запускайте start.bat или:
echo   .venv\Scripts\python.exe app.py --admin-password ВАШ_ПАРОЛЬ
echo.
pause
exit /b

:fail
echo.
echo Не удалось создать venv. Проверьте, что установлен Python 3.12:
echo   py -0p
echo.
pause
