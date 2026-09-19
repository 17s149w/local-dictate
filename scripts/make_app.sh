#!/bin/bash
# Build ~/Applications/LocalDictate.app — a Spotlight-launchable wrapper around src/app.py.
#
# The bundle just runs the repo's venv Python as a child process, so there is
# nothing to "rebuild" when the code changes; re-run this script only if the
# repo moves. macOS TCC permissions (Mic / Input Monitoring / Accessibility /
# Full Disk Access) attach to the bundle and are granted once —
# see docs/PERMISSIONS.md.
#
# NOTE: re-running this script re-signs the bundle ad-hoc, which INVALIDATES
# existing TCC grants. After running make_app.sh you must re-grant Full Disk
# Access (and the other three permissions if prompted) in System Settings.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$HOME/Applications/LocalDictate.app"

mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleName</key><string>Local Dictate</string>
    <key>CFBundleDisplayName</key><string>Local Dictate</string>
    <key>CFBundleIdentifier</key><string>local.sylvan.localdictate</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleExecutable</key><string>localdictate</string>
    <key>LSUIElement</key><true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Local Dictate records dictation audio while the hotkey is active.</string>
</dict>
</plist>
PLIST

# Unquoted heredoc: $ROOT is baked in at build time so Spotlight launches
# (which have no repo context) find the venv and the code.
cat > "$APP/Contents/MacOS/localdictate" <<LAUNCH
#!/bin/bash
# ---------------------------------------------------------------------------
# ALWAYS-WRITABLE launch log (never under ~/Documents — TCC allows ~/Library).
# Written at every launch attempt, even ones that abort early, so silent
# failures become diagnosable.
# ---------------------------------------------------------------------------
LAUNCH_LOG="\$HOME/Library/Logs/LocalDictate/launch.log"
mkdir -p "\$(dirname "\$LAUNCH_LOG")"
TS="\$(date '+%Y-%m-%d %H:%M:%S')"
echo "\$TS [launch] localdictate launcher starting (ROOT=$ROOT)" >> "\$LAUNCH_LOG"

# ---------------------------------------------------------------------------
# Verify that ROOT is readable BEFORE doing anything else.
# Root cause of the "Local Dictate doesn't launch from Spotlight" bug: macOS TCC
# blocks app bundles from reading ~/Documents without Full Disk Access.
#
# Must be a REAL read, not [ -r ]: the test builtin uses access(2), which only
# checks POSIX permission bits and is blind to TCC, so it returns true right
# before open(2) fails. That false pass let python start and die invisibly
# (its stderr was redirected into ~/Documents, which was also blocked).
# ---------------------------------------------------------------------------
if ! head -c 1 "$ROOT/src/app.py" >/dev/null 2>&1; then
    echo "\$TS [launch] ERROR: cannot read $ROOT/src/app.py — Full Disk Access not granted" >> "\$LAUNCH_LOG"
    osascript -e 'display dialog "Local Dictate needs Full Disk Access to start.\n\nFix: System Settings → Privacy & Security → Full Disk Access → \"+\" → select LocalDictate.app\n\nThen relaunch Local Dictate.\n\n(Launch log: ~/Library/Logs/LocalDictate/launch.log)" with title "Local Dictate — Permission Required" buttons {"OK"} default button "OK" with icon caution' 2>/dev/null || true
    exit 1
fi

# ---------------------------------------------------------------------------
# Single-instance guard using a PID file — more reliable than pgrep -f which
# can match unrelated processes sharing the same command-line substring.
# ---------------------------------------------------------------------------
PID_DIR="\$HOME/Library/Application Support/LocalDictate"
PID_FILE="\$PID_DIR/localdictate.pid"
mkdir -p "\$PID_DIR"

if [ -f "\$PID_FILE" ]; then
    OLD_PID="\$(cat "\$PID_FILE" 2>/dev/null)"
    if [ -n "\$OLD_PID" ] && kill -0 "\$OLD_PID" 2>/dev/null; then
        echo "\$TS [launch] already running as PID \$OLD_PID — aborting duplicate launch" >> "\$LAUNCH_LOG"
        exit 0
    else
        echo "\$TS [launch] stale PID file (PID \$OLD_PID not running) — removing" >> "\$LAUNCH_LOG"
        rm -f "\$PID_FILE"
    fi
fi

mkdir -p "$ROOT/logs"
echo "\$TS [launch] starting python child process" >> "\$LAUNCH_LOG"

# Run python as a CHILD (no exec): exec-replacing the bundle's main executable
# mid-launch broke the WindowServer check-in and the menu-bar icon's window
# never got laid out (zero-height frame). As a child it connects like a
# terminal-launched process, which works; TCC still attributes to LocalDictate.app.
"$ROOT/.venv/bin/python" -u "$ROOT/src/app.py" >> "$ROOT/logs/app.log" 2>&1 &
CHILD_PID=\$!

echo "\$CHILD_PID" > "\$PID_FILE"
echo "\$TS [launch] python child started as PID \$CHILD_PID" >> "\$LAUNCH_LOG"

# Wait for the child so the bundle process stays alive while the app runs.
# This keeps the TCC grant attributed to LocalDictate.app rather than to bash.
wait \$CHILD_PID
EXIT_CODE=\$?

echo "\$(date '+%Y-%m-%d %H:%M:%S') [launch] python child exited with code \$EXIT_CODE" >> "\$LAUNCH_LOG"
rm -f "\$PID_FILE"
LAUNCH
chmod +x "$APP/Contents/MacOS/localdictate"

codesign --force -s - "$APP"
echo "Built $APP"
echo "IMPORTANT: re-running this script invalidates existing TCC grants."
echo "You must re-grant Full Disk Access (and other permissions) after each rebuild."
echo "See docs/PERMISSIONS.md for the full permission setup."
echo "First launch: open it once, then grant permissions per docs/PERMISSIONS.md."
