@echo off
REM ============================================================
REM Build script for Simple SFTP Client - standalone Windows app
REM Author: JDE-Projects
REM ============================================================
REM Installs the pinned deps from requirements.txt (pywebview + PySide6,
REM paramiko + crypto stack, keyring, PyInstaller), then builds a standalone
REM --onedir app. The resulting dist\ folder runs on any Windows PC with no
REM Python or other software installed.
REM
REM Qt binding: PySide6 (LGPL), NOT PyQt6 (GPL). QT_API=pyside6 makes any
REM qtpy import bind PySide6. --onedir keeps the bundled LGPL Qt replaceable.
REM ============================================================
cd /d "%~dp0"

REM --- skip interactive pauses when running in CI (GitHub Actions sets CI) ---
set "PAUSE=pause"
if defined CI set "PAUSE="

REM --- force the LGPL Qt binding for any qtpy import during the build ---
set QT_API=pyside6

echo.
echo ============================================================
echo   Simple SFTP Client - Standalone App Builder
echo ============================================================
echo.

REM --- check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Install Python 3 from https://python.org and tick "Add Python to PATH".
    %PAUSE%
    exit /b 1
)

echo [1/3] Installing pinned dependencies from requirements.txt ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies from requirements.txt.
    %PAUSE%
    exit /b 1
)

REM --- clean previous output for a fresh build ---
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "Simple SFTP Client.spec" del /q "Simple SFTP Client.spec"

echo [2/3] Building standalone app (--onedir) ... this may take a few minutes.
python -m PyInstaller --noconfirm --onedir --windowed --name "Simple SFTP Client" ^
    --icon "simple_sftp_client.ico" ^
    --splash "simple_sftp_client-splash.png" ^
    --add-data "simple_sftp_client-UI.html;." ^
    --add-data "simple_sftp_client.png;." ^
    --add-data "fonts;fonts" ^
    --collect-all PySide6 ^
    --collect-all qtpy ^
    --collect-all keyring ^
    --collect-all webview ^
    simple_sftp_client.py
if errorlevel 1 (
    echo ERROR: Build failed. Read the last lines above for the cause.
    %PAUSE%
    exit /b 1
)

echo [3/3] Done.
echo.
echo ============================================================
echo   BUILD SUCCESSFUL!
echo ============================================================
echo.
echo   dist\Simple SFTP Client\Simple SFTP Client.exe
echo.
echo Distribute the WHOLE "Simple SFTP Client" folder (zip it).
echo ============================================================
echo.
%PAUSE%
