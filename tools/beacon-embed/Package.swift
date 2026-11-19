// swift-tools-version: 6.0
import PackageDescription

// Beacon's on-device eyes: SigLIP 2 (text and pictures in one space) for search by meaning,
// Apple Vision's feature print for similar pictures.
let package = Package(
    name: "beacon-embed",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/huggingface/swift-transformers.git", exact: "1.3.3")
    ],
    targets: [
        .executableTarget(
            name: "beacon-embed",
            dependencies: [.product(name: "Tokenizers", package: "swift-transformers")],
            path: "Sources",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
