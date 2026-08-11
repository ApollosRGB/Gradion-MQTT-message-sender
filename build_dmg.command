#!/bin/bash
# ============================================================
#  Builds MQTT Trigger into an installable macOS disk image.
#  Double-click this file in Finder, or run ./build_dmg.command
#
#  Output:  dist/MQTT-Trigger-<version>-macOS-<arch>.dmg
#
#  The .dmg opens to a window with the app on the left and an
#  Applications shortcut on the right - drag one onto the other
#  to install, the way Mac software normally works.
#
#  Must be run on a Mac. A .dmg cannot be built on Windows.
# ============================================================

cd "$(dirname "$0")" || exit 1

pause_exit() {
    if [ -z "$MQTT_TRIGGER_NO_PAUSE" ]; then
        read -r -p "Press Return to close..."
    fi
    exit "${1:-0}"
}

if [ "$(uname -s)" != "Darwin" ]; then
    echo "[ERROR] Disk images can only be built on macOS."
    echo "        On Windows, run build_exe.bat for a .exe instead."
    pause_exit 1
fi

echo "============================================================"
echo "  Building MQTT Trigger.dmg"
echo "============================================================"
echo

# --- Build the .app first ------------------------------------------------
# build_app.command does the Python detection, dependency install and
# PyInstaller run. NO_PAUSE keeps it from stopping for a keypress.
chmod +x build_app.command 2>/dev/null
MQTT_TRIGGER_NO_PAUSE=1 ./build_app.command || {
    echo
    echo "[ERROR] The app build failed, so there is nothing to package."
    pause_exit 1
}

APP="dist/MQTT Trigger.app"
if [ ! -d "$APP" ]; then
    echo "[ERROR] Expected $APP but it is not there."
    pause_exit 1
fi

# --- Work out the output name --------------------------------------------
PY=""
for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 && { PY="$candidate"; break; }
done
VERSION=$("$PY" -c 'import re,pathlib; print(re.search(r"APP_VERSION = \"([^\"]+)\"", pathlib.Path("mqtt_trigger_app.py").read_text(encoding="utf-8")).group(1))' 2>/dev/null || echo "1.3")
ARCH=$(uname -m)
DMG="dist/MQTT-Trigger-${VERSION}-macOS-${ARCH}.dmg"

# --- Lay out what the mounted image will show ----------------------------
STAGE="$(mktemp -d)/MQTT Trigger"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"     # the drag-here target

echo
echo "Packing the disk image..."
rm -f "$DMG"
hdiutil create \
    -volname "MQTT Trigger" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    "$DMG" || {
    echo
    echo "[ERROR] hdiutil could not create the disk image."
    rm -rf "$(dirname "$STAGE")"
    pause_exit 1
}
rm -rf "$(dirname "$STAGE")"

SIZE=$(du -h "$DMG" | cut -f1)

echo
echo "============================================================"
echo "  Done."
echo
echo "    Disk image:  $(pwd)/$DMG   ($SIZE, built for $ARCH)"
echo
echo "  To install: open the .dmg and drag MQTT Trigger onto the"
echo "  Applications folder shown next to it."
echo
echo "  IMPORTANT - the app is not code-signed, so the first launch"
echo "  is blocked by Gatekeeper. To open it:"
echo "      right-click the app -> Open -> Open"
echo "  If macOS still refuses, clear the quarantine flag:"
echo "      xattr -dr com.apple.quarantine \"/Applications/MQTT Trigger.app\""
echo
echo "  This image runs on $ARCH Macs. Build on the other"
echo "  architecture too if you need to cover both."
echo "============================================================"
echo
pause_exit 0
