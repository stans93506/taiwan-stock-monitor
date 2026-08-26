@echo off
cd /d "%~dp0"

:: 載入 .env 環境變數（eol=# 跳過註解行）
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please run setup.bat first.
    pause
    exit /b 1
)

python fetch_revenue.py

if errorlevel 1 (
    echo.
    echo [ERROR] Something went wrong. Please check your internet connection.
    pause
)
