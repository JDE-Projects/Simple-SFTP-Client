@echo off
echo =====================================================
echo  Simple SFTP Client - Build Script
echo =====================================================
echo.
echo Ensuring PySide6 is the bundled Qt binding (not PyQt6)...
set QT_API=pyside6
pip uninstall -y PyQt6 PyQt6-WebEngine >nul 2>&1
echo.
echo Installing build + runtime dependencies...
pip install pyinstaller pywebview PySide6 paramiko keyring
echo.
echo Building executable...
pyinstaller --onedir --windowed --name "Simple SFTP Client" ^
  --icon "simple_sftp_client.ico" ^
  --splash "simple_sftp_client-splash.png" ^
  --add-data "simple_sftp_client-UI.html;." ^
  --add-data "simple_sftp_client.png;." ^
  --add-data "fonts;fonts" ^
  --collect-all PySide6 ^
  --collect-all qtpy ^
  --collect-all keyring ^
  simple_sftp_client.py
echo.
echo =====================================================
echo  Done. Your build is in:  dist\Simple SFTP Client\
echo  Run:  dist\Simple SFTP Client\Simple SFTP Client.exe
echo =====================================================
echo.
pause
