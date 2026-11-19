import SwiftUI
import ImageIO

struct PicksWidgetView: View {
    let snapshot: WidgetSnapshot
    let small: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: small ? 10 : 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(snapshot.updated == nil ? "—" : "\(snapshot.count)").font(.system(size: small ? 30 : 24, weight: .semibold, design: .rounded))
                Text("today’s picks").font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.quiet)
            }
            if snapshot.updated == nil {
                Text("Open Beacon to see your picks.").font(.system(size: 14)).foregroundStyle(Theme.quiet)
            } else if snapshot.picks.isEmpty {
                Text("All clear. Something good will turn up.").font(.system(size: 14)).foregroundStyle(Theme.quiet)
            } else {
                ForEach(Array(snapshot.picks.prefix(small ? 1 : 3))) { card in
                    HStack(spacing: 9) {
                        Circle().fill(Theme.color(slug: card.area) ?? Theme.quiet).frame(width: 6, height: 6)
                        Text(card.headline).font(.system(size: small ? 14 : 13, weight: .medium))
                            .lineLimit(small ? 3 : 1).frame(maxWidth: .infinity, alignment: .leading)
                        if let image = widgetImage(card.jpeg) {
                            Image(decorative: image, scale: 1).resizable().scaledToFill()
                                .frame(width: small ? 38 : 30, height: small ? 48 : 30).clipped()
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(Theme.ink)
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Theme.page)
        .accessibilityElement(children: .combine)
    }
}

struct LibraryWidgetView: View {
    let card: WidgetCard?

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .bottomLeading) {
                if let card, let image = widgetImage(card.jpeg) {
                    Image(decorative: image, scale: 1).resizable().scaledToFill()
                        .frame(width: geometry.size.width, height: geometry.size.height).clipped()
                    LinearGradient(colors: [.clear, .black.opacity(0.75)], startPoint: .center, endPoint: .bottom)
                    VStack(alignment: .leading, spacing: 5) {
                        Text("FROM YOUR LIBRARY").font(.system(size: 9, weight: .semibold)).tracking(1)
                        Text(card.headline).font(.system(size: 13, weight: .medium)).lineLimit(2)
                    }
                    .foregroundStyle(.white).padding(16)
                } else {
                    Theme.page
                    VStack(alignment: .leading, spacing: 8) {
                        Text("From your library").font(.system(size: 17, weight: .semibold))
                        Text("Open Beacon to bring back a picture you saved.").font(.system(size: 14)).foregroundStyle(Theme.quiet)
                    }.foregroundStyle(Theme.ink).padding(16)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

func widgetImage(_ data: Data?) -> CGImage? {
    guard let data, let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}
