@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean Paperwork.spec
if errorlevel 1 exit /b 1
echo.
echo Built dist\Paperwork\Paperwork.exe. Keep the entire Paperwork folder together.
pause
