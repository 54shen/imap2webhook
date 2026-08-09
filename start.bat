@echo off
REM NOTE: keep this file ASCII-only. Chinese text in a .bat gets mangled
REM by cmd.exe depending on the system codepage (UTF-8 vs GBK).
setlocal
cd /d "%~dp0"

echo ============================================
echo   imap2webhook launcher (Windows)
echo ============================================
echo.

REM ---------- Step 1: virtual environment ----------
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create venv. Install Python 3.10+ first.
        pause
        exit /b 1
    )
) else (
    echo [1/3] Virtual environment ready
)

REM ---------- Step 2: install dependencies ----------
echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies. Check your network.
    pause
    exit /b 1
)

REM ---------- Step 3: config check ----------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [WARN] .env created from template - please fill it in first!
)

set NEED_FIX=0
findstr /C:"IMAP_HOST=CHANGE_ME_" ".env" >nul 2>&1 && set NEED_FIX=1
findstr /C:"IMAP_USER=CHANGE_ME_" ".env" >nul 2>&1 && set NEED_FIX=1
findstr /C:"IMAP_PWD=CHANGE_ME_" ".env" >nul 2>&1 && set NEED_FIX=1
REM WEBHOOK is optional when CUSTOM_SENDER is enabled
findstr /C:"WEBHOOK=CHANGE_ME_" ".env" >nul 2>&1 && (
    findstr /B /C:"CUSTOM_SENDER=" ".env" >nul 2>&1 || set NEED_FIX=1
)

if "%NEED_FIX%"=="1" (
    echo.
    echo [WARN] Missing config in .env:
    echo        - IMAP_HOST / IMAP_USER / IMAP_PWD are required
    echo        - WEBHOOK is required unless CUSTOM_SENDER is enabled
    echo        Edit .env and run again.
    echo.
    pause
    exit /b 1
)

REM ---------- Step 4: launch ----------
if not exist "data" mkdir data
echo.
echo [3/3] Starting service... (press Ctrl+C to stop)
echo        DB: %CD%\data\data.db
echo.
REM -m app.main: run as a module so the project root is on sys.path
REM (running app/main.py directly breaks "import app..." on Windows)
".venv\Scripts\python.exe" -m app.main

echo.
echo Service stopped.
pause
