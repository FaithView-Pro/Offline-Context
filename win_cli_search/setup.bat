@echo off
echo ============================================
echo  FaithView Pro Bible Vector Search Setup
echo ============================================
echo.

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not on PATH.
    echo Download from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo Python found: 
python --version
echo.

:: Check for pip
pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: pip is not available.
    pause
    exit /b 1
)

echo Installing dependencies...
echo This will download ~130 MB for the bge-small-en-v1.5 model.
echo Internet connection required for first install only.
echo.
pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo ERROR: pip install failed. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo Quick test:
echo   python search.py "the Lord is my shepherd"
echo.
echo Interactive mode:
echo   python search.py -i
echo.
pause
