import CryptoKit
import ImageIO
import SwiftUI
import UniformTypeIdentifiers

/// Library pictures at tile size: downloaded once, shrunk with ImageIO off the main thread, kept in
/// memory and saved to disk as small JPEGs, so scrolling never decodes a full-size picture and the
/// next launch reads a 40 KB file instead of the original. (AsyncImage decoded every X picture at
/// full size, often 2,000 px and up, while scrolling.)
actor Thumbnails {
    static let shared = Thumbnails()
    private let memory = NSCache<NSString, CGImage>()
    private var loading: [String: Task<CGImage?, Never>] = [:]

    /// ~/Library/Caches/<bundle id>/Thumbnails; the system may empty it, and that's fine. (On the Mac
    /// the caches folder is shared by every app, so the bundle id keeps ours apart.)
    static let folder: URL = {
        let folder = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "Beacon", isDirectory: true)
            .appendingPathComponent("Thumbnails", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return folder
    }()

    init() { memory.totalCostLimit = 256 << 20 }

    static func file(for key: String) -> URL {
        let name = SHA256.hash(data: Data(key.utf8)).prefix(16).map { String(format: "%02x", $0) }.joined()
        return folder.appendingPathComponent(name + ".jpg")
    }

    static func shrink(_ source: CGImageSource, pixels: Int) -> CGImage? {
        CGImageSourceCreateThumbnailAtIndex(source, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,  // decode now, here, not on the main thread when drawn
            kCGImageSourceThumbnailMaxPixelSize: pixels,
        ] as CFDictionary)
    }

    /// The picture at `url`, no larger than `pixels` on its longest side.
    func image(_ url: URL, pixels: Int) async -> CGImage? {
        let key = "\(pixels)|\(url.absoluteString)"
        if let image = memory.object(forKey: key as NSString) { return image }
        if let task = loading[key] { return await task.value }
        let task = Task.detached(priority: .userInitiated) { () -> CGImage? in
            let file = Self.file(for: key)
            if let source = CGImageSourceCreateWithURL(file as CFURL, nil), let image = Self.shrink(source, pixels: pixels) {
                return image  // made on an earlier launch
            }
            guard let (data, _) = try? await URLSession.shared.data(from: url),
                  let source = CGImageSourceCreateWithData(data as CFData, nil),
                  let image = Self.shrink(source, pixels: pixels) else { return nil }
            if let destination = CGImageDestinationCreateWithURL(file as CFURL, UTType.jpeg.identifier as CFString, 1, nil) {
                CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
                CGImageDestinationFinalize(destination)
            }
            return image
        }
        loading[key] = task
        let image = await task.value
        loading[key] = nil
        if let image { memory.setObject(image, forKey: key as NSString, cost: image.bytesPerRow * image.height) }
        return image
    }

    /// Already in memory: shown in the same frame, without a fade.
    nonisolated func cached(_ url: URL, pixels: Int) -> CGImage? {
        memory.object(forKey: "\(pixels)|\(url.absoluteString)" as NSString)
    }
}

/// A Library tile's picture, filled to its frame.
struct TileImage: View {
    let url: URL?
    /// Longest side in pixels; tiles are about 300 points wide on a 2x screen.
    var pixels = 640
    /// Fade in when it arrives; off while scrolling, where a fade per tile costs frames.
    var fade = true
    /// Shown until the picture arrives.
    var background = Theme.shelfSide
    @State private var image: CGImage?

    var body: some View {
        ZStack {
            background
            if let image {
                Image(decorative: image, scale: 1).resizable().scaledToFill()
                    .transition(.opacity)
                    .onAppear {
                        #if !BEACON_WIDGET
                        Bench.pictureShown()
                        #endif
                    }
            }
        }
        .task(id: url) {
            guard let url else { return }
            if let cached = Thumbnails.shared.cached(url, pixels: pixels) {
                image = cached
                return
            }
            let loaded = await Thumbnails.shared.image(url, pixels: pixels)
            if fade { withAnimation(.easeOut(duration: 0.25)) { image = loaded } } else { image = loaded }
        }
    }
}
