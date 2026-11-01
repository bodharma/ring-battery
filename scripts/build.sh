#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-"$ROOT/.venv/bin/python"}"
ARCH="$(uname -m)"
case "$ARCH" in arm64|x86_64) ;; *) echo "Unsupported build architecture: $ARCH" >&2; exit 1 ;; esac
if [[ ! -x "$PYTHON" ]]; then
    echo 'Create .venv and install requirements-build.txt before building.' >&2
    exit 1
fi
mkdir -p "$ROOT/.build" "$ROOT/dist"
WORK="$(mktemp -d "$ROOT/.build/package.XXXXXX")"
APP="$WORK/Ring Battery.app"
READER="$APP/Contents/Resources/Reader"
mkdir -p "$APP/Contents/MacOS" "$READER" "$WORK/home"

PYINSTALLER_CONFIG_DIR="$WORK/cache" MACOSX_DEPLOYMENT_TARGET=15.0 "$PYTHON" -m PyInstaller \
    --noconfirm --clean --onedir --target-architecture "$ARCH" --name ring-reader \
    --distpath "$WORK/reader" --workpath "$WORK/pyinstaller" --specpath "$WORK" \
    --paths "$ROOT/backend/vendor" --add-data "$ROOT/backend/vendor:vendor" \
    --collect-submodules bleak.backends.corebluetooth \
    --copy-metadata bleak --copy-metadata pycryptodome \
    --copy-metadata pyobjc-framework-Cocoa --copy-metadata pyobjc-framework-CoreBluetooth \
    --hidden-import Foundation --hidden-import CoreBluetooth --hidden-import objc \
    "$ROOT/backend/read_ring.py"
/usr/bin/ditto "$WORK/reader/ring-reader" "$READER"
/usr/bin/swiftc -O -assert-config Debug -target "$ARCH-apple-macos15.0" \
    -framework Cocoa -framework UserNotifications -framework CoreBluetooth -framework Security -framework ServiceManagement \
    "$ROOT/Sources/RingBattery/main.swift" -o "$APP/Contents/MacOS/RingBattery"
cp "$ROOT/packaging/Info.plist" "$APP/Contents/Info.plist"
mkdir -p "$APP/Contents/Resources/Licenses"
cp "$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')/LICENSE.txt" "$APP/Contents/Resources/Licenses/Python.txt"

# PyInstaller signs collected libraries; re-sign nested code before sealing the app.
while IFS= read -r -d '' binary; do
    if [[ "$(/usr/bin/file -b "$binary")" == *Mach-O* ]]; then
        /usr/bin/codesign --force --sign - "$binary"
    fi
done < <(find "$READER" -type f -print0)
find "$READER" -depth -type d -name '*.framework' -exec /usr/bin/codesign --force --sign - {} \;
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
/usr/bin/plutil -lint "$APP/Contents/Info.plist"

# A cleared environment proves the reader needs no installed Python or source tree.
/usr/bin/env -i HOME="$WORK/home" PATH=/usr/bin:/bin "$READER/ring-reader" --self-test \
    | "$PYTHON" -c 'import json, sys; result = json.load(sys.stdin); assert result.get("ok") is True and result.get("self_test") is True, result; print("Bundled reader self-test passed")'
/usr/bin/env -i HOME="$WORK/home" PATH=/usr/bin:/bin "$APP/Contents/MacOS/RingBattery" --self-test
/usr/bin/ditto -c -k --keepParent "$APP" "$WORK/Ring-Battery-$ARCH.zip"
mv -f "$WORK/Ring-Battery-$ARCH.zip" "$ROOT/dist/Ring-Battery-$ARCH.zip"
(cd "$ROOT/dist" && /usr/bin/shasum -a 256 "Ring-Battery-$ARCH.zip" > "Ring-Battery-$ARCH.zip.sha256")
printf 'App: %s\nArchive: %s\n' "$APP" "$ROOT/dist/Ring-Battery-$ARCH.zip"
