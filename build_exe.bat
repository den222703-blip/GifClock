@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
echo Installing PyQt6 and PyInstaller...
%PY% -m pip install --upgrade PyQt6 pyinstaller
if errorlevel 1 goto fail
echo Building exe...
%PY% -m PyInstaller --noconsole --onefile --clean --name GifClock --add-data "assets;assets" main.py
if errorlevel 1 goto fail
xcopy "assets" "dist\assets" /E /I /Y >nul
echo.
echo DONE: dist\GifClock.exe  (keep the dist\assets folder next to it)
pause
exit /b 0
:fail
echo.
echo BUILD FAILED. Check that Python is installed and added to PATH.
pause
exit /b 1
