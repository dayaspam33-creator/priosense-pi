#!/bin/bash
# Bouwt een dubbelklikbare macOS-app (TetraMonitor.app) die het programma met de
# juiste Python start. De app is een dunne wrapper: hij gebruikt je bestaande
# Python + modules, dus na 'pip install -r requirements.txt' werkt hij meteen.
#
# Gebruik:   ./make_app.sh            (plaatst in ~/Applications)
#            ./make_app.sh ~/Desktop  (of een andere map)
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/TetraMonitor.app"
# Native arch op bouwmoment vastleggen (deze shell draait native, niet onder
# Rosetta) — nodig omdat 'open' de app onder x86_64 kan starten.
NATIVE_ARCH="$(uname -m)"

# 1) Python vinden die PyQt6 heeft
PYBIN=""
for cand in python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c "import PyQt6, pyqtgraph, numpy" >/dev/null 2>&1; then
        PYBIN="$("$cand" -c 'import sys; print(sys.executable)')"
        break
    fi
done
if [ -z "$PYBIN" ]; then
    echo "❌ Geen Python met PyQt6 gevonden. Doe eerst: pip3 install -r requirements.txt"
    exit 1
fi
echo "🐍 Python: $PYBIN"

# 2) App-structuur
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 3) Launcher
cat > "$APP/Contents/MacOS/TetraMonitor" <<LAUNCH
#!/bin/bash
cd "$REPO"
LOG="\$HOME/Library/Logs/TetraMonitor.log"
echo "=== start \$(date) ===" >> "\$LOG"
# Via 'open' kan de app onder Rosetta (x86_64) starten terwijl numpy/PyQt6 voor
# $NATIVE_ARCH zijn gebouwd → forceer de native architectuur van deze Mac.
exec arch -$NATIVE_ARCH "$PYBIN" "$REPO/tetra_monitor.py" "\$@" >> "\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/TetraMonitor"

# 4) Icoon (faalt stil → generiek icoon)
ICON_KEY=""
if QT_QPA_PLATFORM=offscreen "$PYBIN" "$REPO/make_icon.py" /tmp/_tm_icon.png 1024 >/dev/null 2>&1; then
    SET=/tmp/TetraMonitor.iconset
    rm -rf "$SET"; mkdir -p "$SET"
    for s in 16 32 128 256 512; do
        sips -z $s $s        /tmp/_tm_icon.png --out "$SET/icon_${s}x${s}.png"        >/dev/null 2>&1
        sips -z $((s*2)) $((s*2)) /tmp/_tm_icon.png --out "$SET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
    done
    if iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns" >/dev/null 2>&1; then
        ICON_KEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
    fi
fi

# 5) Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>TetraMonitor</string>
    <key>CFBundleDisplayName</key><string>TetraMonitor</string>
    <key>CFBundleExecutable</key><string>TetraMonitor</string>
    <key>CFBundleIdentifier</key><string>local.tetramonitor</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    $ICON_KEY
    <key>NSHighResolutionCapable</key><true/>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST

echo "✅ Klaar: $APP"
echo "   Dubbelklik 'm, of sleep 'm naar je Dock. Te vinden via Spotlight (⌘-spatie)."
