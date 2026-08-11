@echo off
REM ============================================================
REM  Double-click launcher for mqtt_publisher.py
REM  - Checks Python is installed
REM  - Installs the paho-mqtt library if missing
REM  - Runs the publisher
REM ============================================================

cd /d "%~dp0"
title MQTT Publisher

echo ============================================================
echo   MQTT Publisher
echo   Publishes to your configured topic on a timer
echo ============================================================
echo.
echo   Settings come from environment variables - at minimum
echo   MQTT_HOST and MQTT_TOPIC. See the notes at the top of
echo   mqtt_publisher.py for the full list.
echo.

REM --- Check that Python is available ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Please install Python 3 from:
    echo     https://www.python.org/downloads/
    echo During install, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

REM --- Make sure pip (the installer) is available ---
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo pip not found - setting it up...
    python -m ensurepip --upgrade >nul 2>&1
    python -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not set up pip automatically.
        echo Try reinstalling Python from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" plus "pip" during setup.
        echo.
        pause
        exit /b 1
    )
)

REM --- Make sure the paho-mqtt library is installed ---
echo Checking for the paho-mqtt library...
python -c "import paho.mqtt" >nul 2>&1
if errorlevel 1 (
    echo Installing paho-mqtt ...
    python -m pip install paho-mqtt
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not install paho-mqtt. Check your internet connection.
        echo.
        pause
        exit /b 1
    )
)

echo.
echo Starting publisher.  Close this window or press Ctrl+C to stop.
echo ------------------------------------------------------------
echo.

python mqtt_publisher.py

echo.
echo ------------------------------------------------------------
echo Publisher stopped.
pause
