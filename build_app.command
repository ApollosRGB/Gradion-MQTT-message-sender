#!/bin/bash
# ============================================================
#  Builds MQTT Trigger into a macOS app bundle.
#  Double-click this file in Finder, or run ./build_app.command
#
#  Output:  dist/MQTT Trigger.app
#           dist/MQTT-Trigger-<version>-macOS-<arch>.zip
# ============================================================

cd "$(dirname "$0")" || exit 1

# build_dmg.command calls this script; it sets NO_PAUSE so the run does not
# stop on "Press Return" prompts halfway through.
pause_exit() {
    if [ -z "$MQTT_TRIGGER_NO_PAUSE" ]; then
        read -r -p "Press Return to close..."
    fi
    exit "${1:-0}"
}

echo "============================================================"
echo "  Building MQTT Trigger.app"
echo "============================================================"
echo

# --- Find a usable Python 3 ---------------------------------------------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "[ERROR] Python 3.9 or newer was not found."
    echo
    echo "Install it from https://www.python.org/downloads/macos/"
    echo "(the python.org build is recommended - it ships a modern Tk that"
    echo " this app needs to look right)."
    echo
    pause_exit 1
fi

echo "Using $($PY --version) at $(command -v $PY)"

# --- Warn about the ancient Tk in Apple's system Python ------------------
TK_VERSION=$("$PY" -c 'import tkinter; print(tkinter.TkVersion)' 2>/dev/null)
if [ -z "$TK_VERSION" ]; then
    echo
    echo "[ERROR] This Python has no tkinter, so the GUI cannot be built."
    echo "Install Python from https://www.python.org/downloads/macos/ and retry."
    echo
    pause_exit 1
fi
if [ "$(printf '%s\n' "8.6" "$TK_VERSION" | sort -V | head -n1)" != "8.6" ]; then
    echo
    echo "[WARNING] Tk $TK_VERSION detected. Tk 8.6+ is strongly recommended -"
    echo "          older versions render the interface poorly on macOS."
    echo "          Install Python from python.org if the app looks wrong."
    echo
fi

# --- Dependencies --------------------------------------------------------
echo
echo "Installing / checking build dependencies..."
"$PY" -m pip install --upgrade -r requirements.txt pyinstaller || {
    echo
    echo "[ERROR] Could not install dependencies. Check your internet connection."
    echo "        If pip complains about permissions, try:"
    echo "            $PY -m pip install --user -r requirements.txt pyinstaller"
    echo
    pause_exit 1
}

# --- Use icon.icns if the user dropped one next to this script -----------
ICON_ARG=()
if [ -f "icon.icns" ]; then
    ICON_ARG=(--icon "icon.icns")
    echo "Using icon.icns"
fi

echo
echo "Running PyInstaller (this takes a minute)..."
echo

"$PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "MQTT Trigger" \
    --osx-bundle-identifier "com.gradion.mqtttrigger" \
    "${ICON_ARG[@]}" \
    --collect-all customtkinter \
    --collect-submodules keyring.backends \
    --hidden-import keyring.backends.macOS \
    --hidden-import paho.mqtt.client \
    --hidden-import cryptography.fernet \
    --hidden-import cryptography.hazmat.primitives.kdf.pbkdf2 \
    mqtt_trigger_app.py || {
    echo
    echo "[ERROR] Build failed. See the messages above."
    echo
    pause_exit 1
}

# --- Zip the bundle for distribution -------------------------------------
VERSION=$("$PY" -c 'import re,pathlib; print(re.search(r"APP_VERSION = \"([^\"]+)\"", pathlib.Path("mqtt_trigger_app.py").read_text(encoding="utf-8")).group(1))' 2>/dev/null || echo "2.3.0")
ARCH=$(uname -m)
ZIP_NAME="MQTT-Trigger-${VERSION}-macOS-${ARCH}.zip"

if [ -d "dist/MQTT Trigger.app" ]; then
    echo
    echo "Zipping the app bundle..."
    (cd dist && rm -f "$ZIP_NAME" && ditto -c -k --keepParent "MQTT Trigger.app" "$ZIP_NAME")
fi

echo
echo "============================================================"
echo "  Done."
echo
echo "    App:  $(pwd)/dist/MQTT Trigger.app"
if [ -f "dist/$ZIP_NAME" ]; then
    echo "    Zip:  $(pwd)/dist/$ZIP_NAME   (built for $ARCH)"
fi
echo
echo "  Drag the .app into /Applications to install it."
echo
echo "  IMPORTANT - the app is not code-signed, so the first launch"
echo "  is blocked by Gatekeeper. To open it:"
echo "      right-click the app -> Open -> Open"
echo "  If macOS still refuses, clear the quarantine flag:"
echo "      xattr -dr com.apple.quarantine \"/Applications/MQTT Trigger.app\""
echo "============================================================"
echo
pause_exit 0
