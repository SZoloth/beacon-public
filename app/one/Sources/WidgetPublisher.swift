import WidgetKit

/// Both loads merge into the same snapshot on the main actor; a slow image cannot overwrite newer picks.
@MainActor
enum WidgetPublisher {
    private static var picksGeneration = 0
    private static var libraryGeneration = 0
    static func picks(_ items: [Item]) async {
        #if os(iOS)
        guard let file = WidgetSnapshot.file else { return }
        picksGeneration += 1
        let mine = picksGeneration
        var snapshot = WidgetSnapshot.read(from: file)
        snapshot.updated = Date()
        snapshot.count = items.count
        snapshot.picks = items.prefix(3).map {
            WidgetCard(id: $0.id, headline: $0.headline, area: $0.bestProject?.slug)
        }
        do { try snapshot.write(to: file); WidgetCenter.shared.reloadAllTimelines() } catch { return }
        var cards = snapshot.picks
        for index in cards.indices {
            if let url = items[index].preview?.media?.first?.imageURL,
               let image = await Thumbnails.shared.image(url, pixels: 320) {
                cards[index].jpeg = WidgetSnapshot.jpeg(image)
            }
        }
        snapshot = WidgetSnapshot.read(from: file)
        guard mine == picksGeneration else { return }
        snapshot.picks = cards
        do { try snapshot.write(to: file); WidgetCenter.shared.reloadAllTimelines() } catch { return }
        #endif
    }

    static func library(_ entries: [LibraryPage.Entry]) async {
        #if os(iOS)
        guard let file = WidgetSnapshot.file else { return }
        libraryGeneration += 1
        let mine = libraryGeneration
        var cards: [WidgetCard] = []
        for entry in entries.filter({ $0.picture != nil }).prefix(6) {
            if let url = entry.picture, let image = await Thumbnails.shared.image(url, pixels: 720),
               let jpeg = WidgetSnapshot.jpeg(image) {
                cards.append(WidgetCard(id: entry.id, headline: entry.caption, area: entry.area, jpeg: jpeg))
            }
        }
        guard mine == libraryGeneration, !cards.isEmpty || entries.isEmpty else { return } // retain pictures through a network outage
        var snapshot = WidgetSnapshot.read(from: file)
        snapshot.pictures = cards
        do { try snapshot.write(to: file); WidgetCenter.shared.reloadAllTimelines() } catch { return }
        #endif
    }
}
