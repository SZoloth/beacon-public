#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
mkdir -p build/module-cache ../docs/widgets
swiftc -swift-version 5 -module-cache-path build/module-cache \
    Shared/WidgetSnapshot.swift Widgets/WidgetViews.swift Sources/Theme.swift \
    ../Sources/BeaconTriage/Model.swift WidgetTools/RenderWidgets.swift \
    -o build/render-widgets
build/render-widgets ../docs/widgets
