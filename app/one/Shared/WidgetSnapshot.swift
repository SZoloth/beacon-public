import Foundation
import ImageIO
import UniformTypeIdentifiers

struct WidgetCard: Codable, Identifiable {
    let id: Int
    let headline: String
    let area: String?
    var jpeg: Data?
}

struct WidgetSnapshot: Codable {
    static let group = "group.com.example.beacon"
    var updated: Date?
    var count = 0
    var picks: [WidgetCard] = []
    var pictures: [WidgetCard] = []

    static var file: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group)?
            .appendingPathComponent("widgets.json")
    }

    static func read(from file: URL?) -> Self {
        guard let file, let data = try? Data(contentsOf: file),
              let snapshot = try? JSONDecoder().decode(Self.self, from: data) else { return Self() }
        return snapshot
    }

    func write(to file: URL) throws {
        try JSONEncoder().encode(self).write(to: file, options: .atomic)
    }

    func picture(at date: Date) -> WidgetCard? {
        guard !pictures.isEmpty else { return nil }
        let slot = Int(date.timeIntervalSince1970 / (3 * 3600))
        return pictures[((slot % pictures.count) + pictures.count) % pictures.count]
    }

    static func jpeg(_ image: CGImage) -> Data? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return data as Data
    }
}

enum BeaconLink: Equatable {
    case triage, save(Int)

    init?(_ url: URL) {
        guard url.scheme == "beacon", url.query == nil, url.fragment == nil else { return nil }
        if url.host == "triage", url.path.isEmpty || url.path == "/" { self = .triage }
        else if url.host == "save", url.pathComponents.count == 2,
                let id = Int(url.lastPathComponent), id > 0 { self = .save(id) }
        else { return nil }
    }
}
