@echo off
setlocal
cd /d "%~dp0"
if exist "dist\Paperwork\Paperwork.exe" (
  start "" "dist\Paperwork\Paperwork.exe"
  exit /b 0
)
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Python 3.12 or later is needed. Install Python, then run this launcher again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, python_multipart, pymupdf, pymupdf4llm, PIL" >nul 2>&1
if errorlevel 1 (
  echo Installing local app dependencies. This first setup needs internet access.
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Setup failed. Check your internet connection and run this launcher again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
