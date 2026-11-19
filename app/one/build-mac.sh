#!/bin/zsh
# Build Beacon.app (the one app) and install it in ~/Applications.
set -euo pipefail
cd "${0:A:h}"

xcodegen generate --quiet
xcodebuild -project Beacon.xcodeproj -scheme BeaconMac -configuration Release -derivedDataPath build/dd build -quiet
if [[ "${1:-}" == "--build-only" ]]; then
    echo "Built build/dd/Build/Products/Release/Beacon.app"
    exit 0
fi
mkdir -p ~/Applications
rm -rf ~/Applications/Beacon.app
cp -R build/dd/Build/Products/Release/Beacon.app ~/Applications/
echo "Installed ~/Applications/Beacon.app"
