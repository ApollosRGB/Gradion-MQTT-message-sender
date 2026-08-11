@echo off
REM ============================================================
REM  Builds MQTT Trigger into a single .exe
REM  Output: dist\MQTT Trigger.exe
REM ============================================================

cd /d "%~dp0"
title Build MQTT Trigger

echo ============================================================
echo   Building MQTT Trigger.exe
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on this computer.
    echo Install Python 3 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Installing / checking build dependencies...
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 (
    echo.
    echo [ERROR] Could not install dependencies. Check your internet connection.
    pause
    exit /b 1
)

REM Use icon.ico next to this script if one exists.
set ICON_ARG=
if exist "icon.ico" set ICON_ARG=--icon "icon.ico"

echo.
echo Running PyInstaller (this takes a minute)...
echo.

python -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "MQTT Trigger" ^
    %ICON_ARG% ^
    --collect-all customtkinter ^
    --collect-submodules keyring.backends ^
    --hidden-import keyring.backends.Windows ^
    --hidden-import win32timezone ^
    --hidden-import paho.mqtt.client ^
    --hidden-import cryptography.fernet ^
    --hidden-import cryptography.hazmat.primitives.kdf.pbkdf2 ^
    mqtt_trigger_app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done.  Your app is here:
echo     %CD%\dist\MQTT Trigger.exe
echo.
echo   Copy that single file anywhere you like - it needs no
echo   Python installed on the machine that runs it.
echo ============================================================
echo.
pause
