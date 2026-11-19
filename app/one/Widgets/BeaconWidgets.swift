import SwiftUI
import WidgetKit

struct BeaconEntry: TimelineEntry {
    let date: Date
    let snapshot: WidgetSnapshot
}

struct BeaconProvider: TimelineProvider {
    func placeholder(in context: Context) -> BeaconEntry { BeaconEntry(date: Date(), snapshot: WidgetSnapshot()) }
    func getSnapshot(in context: Context, completion: @escaping (BeaconEntry) -> Void) {
        completion(BeaconEntry(date: Date(), snapshot: WidgetSnapshot.read(from: WidgetSnapshot.file)))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<BeaconEntry>) -> Void) {
        Task {
            #if os(macOS)
            let snapshot = await serverSnapshot()
            #else
            let snapshot = WidgetSnapshot.read(from: WidgetSnapshot.file)
            #endif
            let now = Date()
            let entries = (0..<8).map { BeaconEntry(date: now.addingTimeInterval(Double($0) * 3 * 3600), snapshot: snapshot) }
            completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(3600))))
        }
    }

    #if os(macOS)
    /// Ad hoc signing cannot grant an App Group. Read the existing server, with a local last-good cache.
    private func serverSnapshot() async -> WidgetSnapshot {
        let file = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("beacon-widgets.json")
        var snapshot = WidgetSnapshot.read(from: file)
        let base = URL(string: "http://127.0.0.1:8765")!
        do {
            let queue: [Item] = try await fetch(base.appendingPathComponent("queue"))
            let items = Item.picks(from: queue)
            snapshot.updated = Date()
            snapshot.count = items.count
            snapshot.picks = []
            for item in items.prefix(3) {
                let jpeg = await picture(item.preview?.media?.first?.imageURL, pixels: 320)
                snapshot.picks.append(WidgetCard(id: item.id, headline: item.headline, area: item.bestProject?.slug, jpeg: jpeg))
            }
            try snapshot.write(to: file)
        } catch { /* Keep the last successful picks when the server is asleep. */ }
        do {
            let page: LibraryPage = try await fetch(base.appendingPathComponent("library").appending(queryItems: [URLQueryItem(name: "limit", value: "200")]))
            var cards: [WidgetCard] = []
            for entry in page.items.filter({ $0.picture != nil }).prefix(6) {
                if let jpeg = await picture(entry.picture, pixels: 720) {
                    cards.append(WidgetCard(id: entry.id, headline: entry.caption, area: entry.area, jpeg: jpeg))
                }
            }
            if !cards.isEmpty || page.items.isEmpty { snapshot.pictures = cards }
            try snapshot.write(to: file)
        } catch { /* A Library failure must not hide fresh picks. */ }
        return snapshot
    }

    private func fetch<T: Decodable>(_ url: URL) async throws -> T {
        let (data, response) = try await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 10))
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func picture(_ url: URL?, pixels: Int) async -> Data? {
        guard let url, let image = await Thumbnails.shared.image(url, pixels: pixels) else { return nil }
        return WidgetSnapshot.jpeg(image)
    }
    #endif
}

struct PicksWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "BeaconPicks", provider: BeaconProvider()) { entry in
            PicksEntryView(entry: entry)
                .widgetURL(URL(string: "beacon://triage"))
                .containerBackground(Theme.page, for: .widget)
        }
        .configurationDisplayName("Today’s picks")
        .description("What Beacon picked for you to explore.")
        .supportedFamilies([.systemSmall, .systemMedium])
        .contentMarginsDisabled()
    }
}

struct PicksEntryView: View {
    @Environment(\.widgetFamily) private var family
    let entry: BeaconEntry
    var body: some View { PicksWidgetView(snapshot: entry.snapshot, small: family == .systemSmall) }
}

struct LibraryWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "BeaconLibrary", provider: BeaconProvider()) { entry in
            let card = entry.snapshot.picture(at: entry.date)
            LibraryWidgetView(card: card)
                .widgetURL(URL(string: card.map { "beacon://save/\($0.id)" } ?? "beacon://triage"))
                .containerBackground(Theme.page, for: .widget)
        }
        .configurationDisplayName("From your library")
        .description("A picture worth seeing again, every three hours.")
        .supportedFamilies([.systemMedium, .systemLarge])
        .contentMarginsDisabled()
    }
}

@main
struct BeaconWidgets: WidgetBundle {
    var body: some Widget { PicksWidget(); LibraryWidget() }
}
