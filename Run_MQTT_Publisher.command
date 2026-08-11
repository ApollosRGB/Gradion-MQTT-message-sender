#!/bin/bash
# ============================================================
#  Double-click launcher for mqtt_publisher.py  (macOS)
#  - Checks Python 3 is installed
#  - Installs the paho-mqtt library if missing
#  - Runs the publisher
#
#  FIRST-TIME SETUP ON MAC:
#  macOS may refuse to run a downloaded .command file until you
#  allow it. If double-clicking does nothing, do this once:
#     1. Open the Terminal app.
#     2. Type:  chmod +x "  (with a trailing space)
#     3. Drag this file into the Terminal window, then press Enter.
#  After that, double-clicking will work.
# ============================================================

# Move into the folder this script lives in
cd "$(dirname "$0")"

echo "============================================================"
echo "  MQTT Publisher"
echo "  Publishes to your configured topic on a timer"
echo ""
echo "  Settings come from environment variables - at minimum"
echo "  MQTT_HOST and MQTT_TOPIC. See the notes at the top of"
echo "  mqtt_publisher.py for the full list."
echo "============================================================"
echo

# --- Find Python 3 ---
if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "[ERROR] Python 3 was not found on this Mac."
    echo
    echo "Install it from https://www.python.org/downloads/"
    echo "or run:  brew install python"
    echo
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

# --- Make sure pip is available ---
if ! $PY -m pip --version >/dev/null 2>&1; then
    echo "pip not found - setting it up..."
    $PY -m ensurepip --upgrade >/dev/null 2>&1
fi

# --- Make sure the paho-mqtt library is installed ---
echo "Checking for the paho-mqtt library..."
if ! $PY -c "import paho.mqtt" >/dev/null 2>&1; then
    echo "Installing paho-mqtt ..."
    if ! $PY -m pip install --user paho-mqtt; then
        echo
        echo "[ERROR] Could not install paho-mqtt. Check your internet connection."
        echo
        read -n 1 -s -r -p "Press any key to close..."
        exit 1
    fi
fi

echo
echo "Starting publisher.  Press Ctrl+C (or close this window) to stop."
echo "------------------------------------------------------------"
echo

$PY mqtt_publisher.py

echo
echo "------------------------------------------------------------"
echo "Publisher stopped."
read -n 1 -s -r -p "Press any key to close..."
