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
    /// From the Mac's Pair iPhone QR code. Both values travel together, and the server must be on
    /// the tailnet, so a stray link can't point the phone (and its key) somewhere else.
    case pair(server: URL, key: String)

    init?(_ url: URL) {
        guard url.scheme == "beacon", url.fragment == nil else { return nil }
        if url.host == "pair", url.path.isEmpty || url.path == "/" {
            guard let pair = Self.pairing(url) else { return nil }
            self = pair
            return
        }
        guard url.query == nil else { return nil }
        if url.host == "triage", url.path.isEmpty || url.path == "/" { self = .triage }
        else if url.host == "save", url.pathComponents.count == 2,
                let id = Int(url.lastPathComponent), id > 0 { self = .save(id) }
        else { return nil }
    }

    private static func pairing(_ url: URL) -> BeaconLink? {
        guard let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              Set(items.map(\.name)) == ["server", "key"], items.count == 2,
              let serverText = items.first(where: { $0.name == "server" })?.value,
              let key = items.first(where: { $0.name == "key" })?.value,
              (8...200).contains(key.count), key.allSatisfy({ !$0.isWhitespace }),
              let server = URL(string: serverText), ["http", "https"].contains(server.scheme ?? ""),
              let host = server.host, isTailnet(host) else { return nil }
        return .pair(server: server, key: key)
    }

    /// A MagicDNS name (*.ts.net) or a Tailscale address (100.64.0.0/10).
    static func isTailnet(_ host: String) -> Bool {
        if host.hasSuffix(".ts.net") { return true }
        let parts = host.split(separator: ".").compactMap { Int($0) }
        return parts.count == 4 && parts[0] == 100 && (64...127).contains(parts[1]) && parts.allSatisfy { (0...255).contains($0) }
    }

    /// The link the Mac's QR code carries.
    static func pairURL(server: URL, key: String) -> URL? {
        var components = URLComponents()
        components.scheme = "beacon"
        components.host = "pair"
        components.queryItems = [URLQueryItem(name: "server", value: server.absoluteString), URLQueryItem(name: "key", value: key)]
        return components.url
    }
}
