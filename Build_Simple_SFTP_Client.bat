@echo off
echo =====================================================
echo  Simple SFTP Client - Build Script
echo =====================================================
echo.
echo Installing build + runtime dependencies...
pip install pyinstaller pywebview PyQt6 PyQt6-WebEngine paramiko keyring
echo.
echo Building executable...
pyinstaller --onefile --windowed --name "Simple SFTP Client" ^
  --icon "simple_sftp_client.ico" ^
  --splash "simple_sftp_client-splash.png" ^
  --add-data "simple_sftp_client-UI.html;." ^
  --add-data "simple_sftp_client.png;." ^
  --add-data "fonts;fonts" ^
  --collect-all PyQt6 ^
  --collect-all qtpy ^
  --collect-all keyring ^
  simple_sftp_client.py
echo.
echo =====================================================
echo  Done. Your .exe is in:  dist\Simple SFTP Client.exe
echo =====================================================
echo.
pause
