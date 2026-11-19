#!/bin/zsh
# Build "Beacon Triage.app" and install it in ~/Applications.
set -euo pipefail
cd "${0:A:h}/.."

swift build -c release
APP="build/Beacon Triage.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/BeaconTriage "$APP/Contents/MacOS/BeaconTriage"

swift scripts/make-icon.swift build/AppIcon.iconset
iconutil -c icns build/AppIcon.iconset -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Beacon Triage</string>
  <key>CFBundleDisplayName</key><string>Beacon Triage</string>
  <key>CFBundleIdentifier</key><string>com.example.beacon-triage</string>
  <key>CFBundleExecutable</key><string>BeaconTriage</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP"
mkdir -p ~/Applications
rm -rf ~/Applications/"Beacon Triage.app"
cp -R "$APP" ~/Applications/
echo "Installed ~/Applications/Beacon Triage.app"
