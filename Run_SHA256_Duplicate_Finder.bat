@echo off
setlocal
cd /d "%~dp0"

:: Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.8+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Ensure essential packages are installed silently in background if needed
python -c "import customtkinter, send2trash" >nul 2>&1
if errorlevel 1 (
    echo [*] Setting up required libraries (customtkinter, send2trash, xxhash)...
    python -m pip install -r requirements.txt --quiet
)

:: If arguments were passed via command prompt, execute directly with python
if not "%~1"=="" (
    python "%~dp0sha256_duplicate_finder.py" %*
    exit /b %errorlevel%
)

:: Otherwise launch completely silently via VBScript without keeping console window open
start "" wscript "%~dp0Launch_Finder_Silent.vbs"
exit /b 0
