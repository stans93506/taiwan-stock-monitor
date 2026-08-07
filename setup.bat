@echo off
echo.
echo ================================================
echo  Taiwan Stock Revenue Tool - First Time Setup
echo ================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install from: https://www.python.org/downloads/
    echo Remember to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo [OK] Python found:
python --version
echo.
echo Installing required packages, please wait...
echo.

pip install -r "%~dp0requirements.txt"

if errorlevel 1 (
    echo.
    echo [ERROR] Install failed. Please check your internet connection.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Setup complete! Double-click run.bat to start.
echo ================================================
echo.
pause
