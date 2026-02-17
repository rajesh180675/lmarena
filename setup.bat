@echo off
echo ============================================
echo   LMArena Client - First Time Setup
echo ============================================
echo.

:: Check Python
python --version 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found!
    echo Download from: https://www.python.org/downloads/
    echo Install Python 3.8 or higher
    pause
    exit /b 1
)

echo [1/4] Creating virtual environment...
python -m venv venv
if errorlevel 1 (
    echo [WARN] venv failed, using global pip instead
    goto :install_global
)

echo [2/4] Activating virtual environment...
call venv\Scripts\activate.bat

:install_global
echo [3/4] Installing core dependencies...
pip install --upgrade pip

pip install streamlit
if errorlevel 1 (
    echo [ERROR] streamlit install failed
    pause
    exit /b 1
)

pip install requests
pip install cloudscraper
pip install gradio_client
pip install sseclient-py

echo.
echo [4/4] Attempting optional curl-cffi install...
pip install curl-cffi 2>nul
if errorlevel 1 (
    echo [WARN] curl-cffi failed to install - this is OK
    echo        The app will use cloudscraper instead
)

echo.
echo ============================================
echo   Setup complete!
echo   Run the app with: run.bat
echo ============================================
pause
