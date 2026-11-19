// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "BeaconTriage",
    platforms: [.macOS(.v14)],
    targets: [
        // Swift 5 mode: LinkPresentation types are not Sendable yet.
        .executableTarget(name: "BeaconTriage", path: "Sources/BeaconTriage", swiftSettings: [.swiftLanguageMode(.v5)]),
        .testTarget(name: "BeaconTriageTests", dependencies: ["BeaconTriage"], path: "Tests/BeaconTriageTests",
                    swiftSettings: [.swiftLanguageMode(.v5)]),
    ]
)
